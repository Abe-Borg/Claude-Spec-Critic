"""Deterministic location of an edit's target element. No model calls.

This is the tier that handles the overwhelming majority of edits at zero
cost, because Spec Critic already did the hard part: every element it showed
the review model carries a stable ``element_id`` (``p7``, ``t0r2``,
``s1h0``), and the sidecar records the one a proposal targeted. Locating an
edit is then an index lookup with a text confirmation, not a search.

The resolution ladder, in order:

1. **By element id**, when the sidecar names one and the element's text still
   confirms the target text.
2. **By unique text**, when exactly one element in the document contains it.
3. **By section**, when several elements contain it but only one sits under
   the finding's own section heading — the disambiguation
   ``ParagraphMapping.section_id`` was added for.
4. **Refuse.** Several indistinguishable candidates are ``AMBIGUOUS``; an id
   that resolves to text that no longer matches is ``DRIFTED``; text that is
   nowhere is ``NOT_FOUND``.

Step 4 is the point of the module. An applier that guesses between two
identical clauses in different articles is worse than one that stops, because
the reviewer cannot see what it chose.

**A borrowed locator is not drift.** The sidecar sets
``has_per_file_original=False`` when a multi-file finding's locator fields
came from the merged group's representative rather than this file's own
original. Such an id points into a *different* document, so a mismatch there
is expected, not evidence the spec changed — it falls through to the text
ladder instead of being reported as drift.
"""
from __future__ import annotations

import re

from .models import (
    Candidate,
    EditEntry,
    ElementKind,
    Location,
    LocationStatus,
)
from .textmatch import contains, normalize

_BODY_PARAGRAPH_RE = re.compile(r"^p\d+$")
_TABLE_ROW_RE = re.compile(r"^t\d+r\d+(?:c\d+t\d+r\d+)*$")
_HEADER_FOOTER_RE = re.compile(r"^s\d+[hf]\d+$")

#: How many candidates the AMBIGUOUS detail (and the assist tier) carries.
#: Enough for a reviewer to see the shape of the problem without pasting a
#: whole specification into a receipt.
MAX_REPORTED_CANDIDATES = 8


def classify_element_id(element_id: str | None) -> ElementKind:
    """Which document surface an element id points at.

    Unknown / supplemental ids (text boxes ``tb…``, footnotes ``fn…``,
    endnotes ``en…``, the synthetic ``meta:*`` block delimiters) classify as
    :attr:`ElementKind.UNSUPPORTED`. The extractor surfaces their text so a
    requirement authored there is still *reviewed*; python-docx does not
    model them as editable containers, so this applier reports them instead
    of writing into raw XML it cannot round-trip safely.
    """
    if not element_id:
        return ElementKind.UNSUPPORTED
    if _BODY_PARAGRAPH_RE.match(element_id):
        return ElementKind.BODY_PARAGRAPH
    if _TABLE_ROW_RE.match(element_id):
        return ElementKind.TABLE_ROW
    if _HEADER_FOOTER_RE.match(element_id):
        return ElementKind.HEADER_FOOTER
    return ElementKind.UNSUPPORTED


def build_candidates(extracted_spec) -> list[Candidate]:
    """Every addressable element of an ``ExtractedSpec``, in document order."""
    candidates: list[Candidate] = []
    for mapping in getattr(extracted_spec, "paragraph_map", None) or []:
        element_id = getattr(mapping, "element_id", "") or ""
        if not element_id or element_id.startswith("meta:"):
            continue
        candidates.append(
            Candidate(
                element_id=element_id,
                text=getattr(mapping, "text", "") or "",
                section_id=getattr(mapping, "section_id", "") or "",
                element_type=getattr(mapping, "element_type", "") or "",
                kind=classify_element_id(element_id),
            )
        )
    return candidates


def _by_id(candidates: list[Candidate]) -> dict[str, Candidate]:
    return {candidate.element_id: candidate for candidate in candidates}


def _section_matches(entry_section: str, candidate_section: str) -> bool:
    """Whether a finding's section names the candidate's section heading.

    Compared loosely in one direction only — a finding's ``section`` is often
    a CSI number ("21 13 13") while the heading carries number *and* title
    ("21 13 13 WET-PIPE SPRINKLER SYSTEMS"), so containment either way is a
    match, but an empty section never matches anything.
    """
    left = normalize(entry_section).casefold()
    right = normalize(candidate_section).casefold()
    if not left or not right:
        return False
    return left in right or right in left


def _unsupported(element_id: str, kind: ElementKind) -> Location:
    return Location(
        status=LocationStatus.UNSUPPORTED_ELEMENT,
        element_id=element_id,
        kind=kind,
        detail=(
            f"element {element_id} is a text box, note, or other container "
            "this applier does not write to; apply this edit by hand"
        ),
    )


def locate(entry: EditEntry, candidates: list[Candidate]) -> Location:
    """Locate one entry's target element among a document's candidates."""
    index = _by_id(candidates)
    locator_text = entry.locator_text
    named_id = entry.target_element_id or entry.evidence_element_id

    # --- 1. By element id ------------------------------------------------
    if named_id:
        kind = classify_element_id(named_id)
        candidate = index.get(named_id)
        if candidate is None:
            # An id that is not in this document at all. For a borrowed
            # locator that is expected; otherwise it is drift worth naming.
            if kind is ElementKind.UNSUPPORTED and entry.has_per_file_original:
                return _unsupported(named_id, kind)
        elif kind is ElementKind.UNSUPPORTED:
            return _unsupported(named_id, kind)
        elif not locator_text:
            # ADD against an element id with no anchor text: nothing to
            # confirm against, and the sidecar already validated the shape.
            return Location(
                status=LocationStatus.RESOLVED_BY_ID,
                element_id=named_id,
                kind=kind,
                detail="resolved by element id (ADD with no anchor text)",
            )
        elif contains(candidate.text, locator_text):
            return Location(
                status=LocationStatus.RESOLVED_BY_ID,
                element_id=named_id,
                kind=kind,
                detail="element id resolved and its text confirmed the target",
            )
        elif entry.has_per_file_original:
            # The id is this file's own and its text has changed: real drift.
            # Fall through to the text ladder anyway — the clause may simply
            # have moved — but remember the drift for the refusal message.
            drift_detail = (
                f"element {named_id} no longer contains the target text; it "
                f"now reads {candidate.text[:160]!r}"
            )
            return _locate_by_text(
                entry, candidates, locator_text, drift_detail=drift_detail
            )

    # --- 2/3. By text, then by section ----------------------------------
    if not locator_text:
        return Location(
            status=LocationStatus.NOT_FOUND,
            detail="entry carries neither a usable element id nor locator text",
        )
    return _locate_by_text(entry, candidates, locator_text)


def _locate_by_text(
    entry: EditEntry,
    candidates: list[Candidate],
    locator_text: str,
    *,
    drift_detail: str = "",
) -> Location:
    matches = [
        candidate
        for candidate in candidates
        if contains(candidate.text, locator_text)
    ]
    writable = [c for c in matches if c.kind is not ElementKind.UNSUPPORTED]

    if not matches:
        if drift_detail:
            return Location(
                status=LocationStatus.DRIFTED,
                element_id=entry.target_element_id or entry.evidence_element_id,
                kind=ElementKind.UNSUPPORTED,
                detail=drift_detail
                + " — and the target text is nowhere else in the document",
            )
        return Location(
            status=LocationStatus.NOT_FOUND,
            detail=(
                "the target text does not appear in this document; it may have "
                "been edited since the review that produced this instruction"
            ),
        )

    if not writable:
        return _unsupported(matches[0].element_id, matches[0].kind)

    if len(writable) == 1:
        only = writable[0]
        return Location(
            status=LocationStatus.RESOLVED_BY_UNIQUE_TEXT,
            element_id=only.element_id,
            kind=only.kind,
            detail=(
                "the target text occurs exactly once in the document"
                + (f" (note: {drift_detail})" if drift_detail else "")
            ),
        )

    sectioned = [
        candidate
        for candidate in writable
        if _section_matches(entry.section, candidate.section_id)
    ]
    if len(sectioned) == 1:
        only = sectioned[0]
        return Location(
            status=LocationStatus.RESOLVED_BY_SECTION,
            element_id=only.element_id,
            kind=only.kind,
            detail=(
                f"{len(writable)} elements contain the target text; only "
                f"{only.element_id} sits under section {entry.section!r}"
            ),
        )

    return Location(
        status=LocationStatus.AMBIGUOUS,
        kind=ElementKind.UNSUPPORTED,
        detail=(
            f"{len(writable)} elements contain the target text and the "
            "finding's section does not single one out"
        ),
        candidates=tuple(writable[:MAX_REPORTED_CANDIDATES]),
    )
