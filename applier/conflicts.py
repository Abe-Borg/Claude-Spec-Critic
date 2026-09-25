"""Instructions that disagree about one place in a document (plan WP-06B).

Once every edit of a document is planned against the unmutated document
(:meth:`applier.docx_edit.DocumentEditor.plan`), what each one changes is
known exactly: an EDIT or DELETE changes a span of one paragraph's visible
text, and an ADD puts a new paragraph into one gap between siblings. Two
instructions are about the same place when their spans share a character, or
when their gaps are one gap — an addition after one paragraph and an addition
before the next go to the same place.

* **Identical** instructions for one place — the same action, the same span
  or gap, the same text — are one change. It is written once, and every other
  copy is a ``DUPLICATE`` of it: a finding emitted twice, or a review finding
  and a coordination finding that propose the same fix. An addition also
  takes its anchor's style, numbering, and run formatting, so two additions
  of one text into one gap are identical only when what they take from their
  anchors is too; "after a heading" and "before a list item" are different
  paragraphs for one place.
* **Different** instructions for one place are an ``EDIT_CONFLICT``, every one
  of them: applying one would consume the text another targets, or would put
  new paragraphs in an order nobody chose. Before this, the first instruction
  in the sidecar was written and the rest failed to find their text, so the
  sidecar's order chose the winner.
* Everything else is independent. It is applied in an order that keeps every
  planned span valid — each paragraph's edits from its last span to its first
  — with additions first, so a new paragraph copies its anchor's formatting as
  the review saw it. Ties are broken by content (:attr:`EditEntry.sort_key`),
  never by the sidecar's order.

Nothing here reads a key or a finding id: two entries of one finding at two
places are independent, and two findings' entries at one place are not.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .docx_edit import PlannedEdit


@dataclass(frozen=True)
class Settlement:
    """What happens to each planned edit, by its position in the plan list."""

    #: Positions to apply, in application order.
    order: tuple[int, ...] = ()
    #: Position → the position of the identical edit written in its place.
    duplicates: dict[int, int] = field(default_factory=dict)
    #: Position → the positions of the edits it disagrees with, in content
    #: order.
    conflicts: dict[int, tuple[int, ...]] = field(default_factory=dict)


def _node(element) -> int | None:
    return None if element is None else id(element)


def _identity(plan: PlannedEdit) -> tuple:
    """Two plans with one identity are one change: whichever is written, the
    document is the same. For an addition that includes the formatting the
    new paragraph takes from its anchor, which two anchors of one gap need
    not share."""
    if plan.is_addition:
        before, after = plan.gap
        return (
            "gap",
            _node(before),
            _node(after),
            plan.formatting,
            plan.entry.replacement_text,
        )
    return (
        "span",
        id(plan.paragraph),
        plan.span,
        plan.entry.action_type,
        plan.entry.replacement_text,
    )


def _overlap(first: PlannedEdit, second: PlannedEdit) -> bool:
    """Whether two plans are about the same place (identity aside)."""
    if first.is_addition != second.is_addition:
        return False
    if first.is_addition:
        return tuple(map(_node, first.gap)) == tuple(map(_node, second.gap))
    if first.paragraph is not second.paragraph:
        return False
    (a_start, a_end), (b_start, b_end) = first.span, second.span
    return a_start < b_end and b_start < a_end


def settle(plans: list[PlannedEdit]) -> Settlement:
    """Settle one document's planned edits: apply, write once, or hold."""
    # Identical plans collapse into one class, keyed in first-seen order; the
    # class's members are sorted by content below, so the order they arrived
    # in never picks which one is written.
    classes: dict[tuple, list[int]] = {}
    for position, plan in enumerate(plans):
        classes.setdefault(_identity(plan), []).append(position)
    members = [
        sorted(positions, key=lambda p: plans[p].entry.sort_key)
        for positions in classes.values()
    ]

    conflicting: dict[int, set[int]] = {}
    for i, first in enumerate(members):
        for j in range(i + 1, len(members)):
            second = members[j]
            if _overlap(plans[first[0]], plans[second[0]]):
                conflicting.setdefault(i, set()).add(j)
                conflicting.setdefault(j, set()).add(i)

    conflicts: dict[int, tuple[int, ...]] = {}
    duplicates: dict[int, int] = {}
    primaries: list[int] = []
    for index, positions in enumerate(members):
        if index in conflicting:
            others = sorted(
                (p for other in conflicting[index] for p in members[other]),
                key=lambda p: plans[p].entry.sort_key,
            )
            for position in positions:
                conflicts[position] = tuple(others)
            continue
        primary, *copies = positions
        primaries.append(primary)
        for position in copies:
            duplicates[position] = primary

    return Settlement(
        order=application_order(plans, primaries),
        duplicates=duplicates,
        conflicts=conflicts,
    )


def application_order(plans: list[PlannedEdit], positions: list[int]) -> tuple[int, ...]:
    """An order to apply compatible plans in that keeps every plan valid.

    Additions first, then each paragraph's edits from its last span to its
    first: an edit changes a paragraph's runs only at and after its own
    start, so a span before it keeps its offsets. Paragraphs, and additions,
    follow content order.
    """
    additions = sorted(
        (p for p in positions if plans[p].is_addition),
        key=lambda p: plans[p].entry.sort_key,
    )
    by_paragraph: dict[int, list[int]] = {}
    for position in positions:
        if not plans[position].is_addition:
            by_paragraph.setdefault(id(plans[position].paragraph), []).append(position)
    paragraphs = sorted(
        by_paragraph.values(),
        key=lambda group: min(plans[p].entry.sort_key for p in group),
    )
    inline: list[int] = []
    for group in paragraphs:
        inline.extend(sorted(group, key=lambda p: -plans[p].span[0]))
    return tuple(additions + inline)
