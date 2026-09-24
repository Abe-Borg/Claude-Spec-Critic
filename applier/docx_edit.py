"""Apply one located edit to a ``.docx``, as a Word tracked change by default.

**Why tracked changes are the default.** Spec Critic removed its write-back
stack in v3.0.0 on the argument that silently rewriting a legal document is
the loudest way to hide uncertainty. Writing edits as Word revisions answers
that argument instead of relitigating it: every change lands in the reviewer's
own Accept/Reject UI, attributed to a named author, next to the text it
replaced. The document is not decided — it is marked up. ``--mode direct``
exists for a reviewer who wants a clean file and has already read the report,
and it is opt-in for that reason.

**Why run splitting, and where it stops.** Word stores a sentence as a
sequence of runs broken at every formatting change, so the text a reviewer
sees as one clause is rarely one XML node. To replace exactly the matched
characters and nothing else, this module splits the runs at the match
boundaries and wraps only the covered runs. A run that must be split but
carries a tab, a line break, or several text nodes is **refused** rather than
rebuilt: those carry layout this module cannot reproduce faithfully, and a
silently mangled tab stop in a spec table is exactly the kind of damage that
is noticed three revisions later.

Nothing here writes to disk. :class:`DocumentEditor` mutates an in-memory
``Document``; the caller saves it under a new name so the source file stays
byte-identical.

**Every edit is planned before the first one is written.**
:meth:`DocumentEditor.plan` decides, against the unmutated document, exactly
what an edit changes — the paragraph and the characters, or the place a new
paragraph goes — and raises every refusal this module can raise. A run plans
all of its edits, settles which of them are compatible (``applier.conflicts``),
and only then applies the plans (:meth:`DocumentEditor.apply_planned`). The
plans are applied from the positions they were planned at, so one edit's
replacement can never hide or duplicate the text another edit targets, and
the order the sidecar lists edits in decides nothing.

**Readable is not writable (plan WP-02).** The extractor reads text inside
content controls, smart tags, custom XML elements, hyperlinks, simple fields,
and complex fields' stored results. This writer edits only plain runs that are
direct children of an ordinary paragraph. So a target is matched against the
extractor's own visible text — through the same walk, which records where every
character came from — and the edit is refused, with a reason naming the
container, when any matched character comes through a wrapper, another
author's revision, or a field result; when a wrapper, revision, field
character, or anchored object sits between the matched runs (moving the runs
into a deletion would reorder it); or when the text also appears elsewhere in
the element, inside a wrapper or not. A refusal here is always safe; a guess is
not.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from docx.oxml.ns import qn
from docx.table import Table

from src.input.extractor import (
    _cell_paragraphs,
    _paragraph_segments,
    _row_text_cells,
    _unique_row_cells,
)

from .models import (
    ACTION_ADD,
    ACTION_EDIT,
    INSERT_BEFORE,
    EditEntry,
    ElementKind,
    Location,
)
from .textmatch import count_occurrences, find_span

#: Author recorded on every revision this program writes, so a reviewer can
#: filter Word's review pane by originator.
REVISION_AUTHOR = "Spec Critic Applier"

#: Revision ids only need to be unique within the document. Starting high
#: keeps them clear of ids Word itself assigned to any pre-existing markup.
_REVISION_ID_BASE = 900000

_TABLE_PATH_RE = re.compile(r"^t(\d+)r(\d+)((?:c\d+t\d+r\d+)*)$")
_NESTED_STEP_RE = re.compile(r"c(\d+)t(\d+)r(\d+)")
_HEADER_FOOTER_RE = re.compile(r"^s(\d+)([hf])(\d+)$")
_BODY_PARAGRAPH_RE = re.compile(r"^p(\d+)$")

_REVISION_REFUSAL = (
    "the target text sits inside an existing tracked revision by another "
    "author. Accept or reject that revision in Word first — editing inside an "
    "undecided change would leave the document's history unreadable"
)
_CONTENT_CONTROL_REFUSAL = (
    "the target text sits inside a content control. This applier does not "
    "edit through content controls (a control can be locked, bound to "
    "document data, or showing placeholder text); apply this edit by hand"
)
_FIELD_REFUSAL = (
    "the target text is a field's stored result, which Word regenerates from "
    "the field code; change what the field refers to, or apply this edit by hand"
)

#: Why text reached through each container cannot be written. Keys are the
#: route labels the extractor's walk records (``_paragraph_segments``); the
#: outermost container names the refusal.
_ROUTE_REFUSALS = {
    "ins": _REVISION_REFUSAL,
    "moveTo": _REVISION_REFUSAL,
    "sdt": _CONTENT_CONTROL_REFUSAL,
    "field": _FIELD_REFUSAL,
    "fldSimple": _FIELD_REFUSAL,
    "hyperlink": (
        "the target text sits inside a hyperlink, which this applier does not "
        "edit through (the link could be lost); apply this edit by hand"
    ),
    "smartTag": (
        "the target text sits inside a smart tag, which this applier does not "
        "edit through; apply this edit by hand"
    ),
    "customXml": (
        "the target text sits inside a custom XML element, which this applier "
        "does not edit through; apply this edit by hand"
    ),
    "dir": (
        "the target text sits inside a bidirectional-text container, which this "
        "applier does not edit through; apply this edit by hand"
    ),
    "bdo": (
        "the target text sits inside a bidirectional-text container, which this "
        "applier does not edit through; apply this edit by hand"
    ),
}
_WRAPPED_TEXT_REFUSAL = (
    "the target text sits inside a container this applier does not edit "
    "through; apply this edit by hand"
)
_SPANS_STRUCTURE_REFUSAL = (
    "the target text spans a field, content control, hyperlink, tracked "
    "revision, or anchored object that the edit would have to move; apply this "
    "edit by hand so the document's structure is preserved"
)
_UNSPLITTABLE_REFUSAL = (
    "the edit boundary falls inside a run carrying a tab, a line "
    "break, or multiple text nodes; apply this one by hand so its "
    "layout is preserved"
)

#: Block wrappers a paragraph can sit in (plan WP-02). A paragraph inside one
#: is read for review but never written.
_BLOCK_WRAPPER_TAGS = frozenset({qn("w:sdt"), qn("w:customXml")})

#: Paragraph children that mark a position and carry no content. A tracked
#: edit leaves them where they are, so one inside the matched span only moves
#: slightly relative to the text — the behavior every edit has always had.
_POSITION_MARKERS = frozenset(
    qn(tag)
    for tag in (
        "w:bookmarkStart",
        "w:bookmarkEnd",
        "w:proofErr",
        "w:permStart",
        "w:permEnd",
        "w:commentRangeStart",
        "w:commentRangeEnd",
        "w:moveFromRangeStart",
        "w:moveFromRangeEnd",
        "w:moveToRangeStart",
        "w:moveToRangeEnd",
        "w:customXmlInsRangeStart",
        "w:customXmlInsRangeEnd",
        "w:customXmlDelRangeStart",
        "w:customXmlDelRangeEnd",
        "w:customXmlMoveFromRangeStart",
        "w:customXmlMoveFromRangeEnd",
        "w:customXmlMoveToRangeStart",
        "w:customXmlMoveToRangeEnd",
    )
)

#: Run children that neither carry text nor anchor anything.
_INERT_RUN_CHILDREN = frozenset(qn(tag) for tag in ("w:rPr", "w:lastRenderedPageBreak"))

#: Run children that mark a complex field's structure.
_FIELD_CHARACTERS = frozenset(qn(tag) for tag in ("w:fldChar", "w:instrText"))


class EditError(Exception):
    """This edit cannot be applied safely. The caller reports, never guesses."""


@dataclass(frozen=True)
class EditMode:
    tracked: bool = True

    @property
    def label(self) -> str:
        return "tracked" if self.tracked else "direct"


TRACKED = EditMode(tracked=True)
DIRECT = EditMode(tracked=False)


# --------------------------------------------------------------------------
# Run-level text handling
# --------------------------------------------------------------------------
#: Run children that render as text without being a text node: python-docx
#: translates each (``str()``) the same way the extractor's walk does.
_LAYOUT_TEXT_TAGS = frozenset(
    qn(tag) for tag in ("w:tab", "w:ptab", "w:br", "w:cr", "w:noBreakHyphen")
)


def _run_text(run_el) -> tuple[str, bool]:
    """``(visible_text, splittable)`` for a ``w:r`` element.

    The text is python-docx's own translation (``CT_R.text``) — the text the
    extractor read — so offsets agree with the match. ``splittable`` is False
    when the run carries anything this module cannot reconstruct after a
    split — a tab, a break, a non-breaking hyphen, or more than one text node.
    """
    parts: list[str] = []
    text_nodes = 0
    splittable = True
    for child in run_el:
        tag = child.tag
        if tag == qn("w:t"):
            parts.append(child.text or "")
            text_nodes += 1
        elif tag in _LAYOUT_TEXT_TAGS:
            parts.append(str(child))
            splittable = False
    if text_nodes > 1:
        splittable = False
    return "".join(parts), splittable


def _set_run_text(run_el, text: str) -> None:
    """Replace a run's single text node, preserving significant whitespace."""
    for child in list(run_el):
        if child.tag == qn("w:t"):
            run_el.remove(child)
    node = run_el.makeelement(qn("w:t"), {})
    node.set(qn("xml:space"), "preserve")
    node.text = text
    run_el.append(node)


def _content_runs(p_el) -> list:
    """Direct ``w:r`` children of a paragraph, in order."""
    return [child for child in p_el if child.tag == qn("w:r")]


def _paragraph_text(p_el) -> str:
    return "".join(_run_text(run)[0] for run in _content_runs(p_el))


def _visible_text(segments) -> str:
    return "".join(segment.text for segment in segments)


def _inside_block_wrapper(p_el) -> bool:
    """Whether a paragraph sits inside a block content control or custom XML
    block. Only a table row resolves to such paragraphs (its cells' controls
    are part of the row's text); the writer never edits them."""
    return any(ancestor.tag in _BLOCK_WRAPPER_TAGS for ancestor in p_el.iterancestors())


def _covered_segments(segments, start: int, end: int) -> list:
    """``(segment, local_start, local_end)`` for every segment the visible-text
    span ``[start, end)`` touches, in order."""
    covered = []
    cursor = 0
    for segment in segments:
        segment_start, segment_end = cursor, cursor + len(segment.text)
        cursor = segment_end
        low, high = max(start, segment_start), min(end, segment_end)
        if low < high:
            covered.append((segment, low - segment_start, high - segment_start))
    return covered


def _refuse_unwritable(covered) -> None:
    """Refuse when any matched character came through a wrapper, another
    author's revision, or a field result — naming the outermost container,
    because that is the first thing this writer cannot edit through."""
    for segment, _, _ in covered:
        if segment.route:
            raise EditError(_ROUTE_REFUSALS.get(segment.route[0], _WRAPPED_TEXT_REFUSAL))


def _is_inert_run(element) -> bool:
    """A run with no text and nothing anchored in it (formatting only)."""
    if element.tag != qn("w:r"):
        return False
    for child in element:
        tag = child.tag
        if tag in _INERT_RUN_CHILDREN:
            continue
        if tag == qn("w:t") and not child.text:
            continue
        return False
    return True


def _require_plain_span(p_el, runs: list) -> None:
    """Refuse unless the matched runs can be moved into a deletion without
    reordering anything else.

    A tracked edit moves the matched runs into a new ``w:del`` placed where
    the first of them was. Anything between the first and last matched run
    that is not itself matched — a wrapper, a revision, a field character, a
    footnote reference or drawing — would end up on the far side of the edit,
    and a field whose characters were split apart stops being a field. Only
    position markers and formatting-only runs may sit in between.
    """
    matched = set(runs)
    first, last = runs[0], runs[-1]
    inside = False
    for child in p_el:
        if child is first:
            inside = True
        if inside:
            if child in matched:
                if any(grandchild.tag in _FIELD_CHARACTERS for grandchild in child):
                    raise EditError(_SPANS_STRUCTURE_REFUSAL)
            elif child.tag not in _POSITION_MARKERS and not _is_inert_run(child):
                raise EditError(_SPANS_STRUCTURE_REFUSAL)
        if child is last:
            return


def _direct_span(p_el, covered) -> tuple[int, int]:
    """The matched span in the writer's coordinates: offsets into the
    concatenated text of the paragraph's direct runs (``_paragraph_text``),
    which is what ``_runs_covering`` splits by.

    Offsets carry over unchanged because the writer reads a run's text with
    the same translation the extractor used; a run read differently would put
    the edit on the wrong characters, so it is refused instead.
    """
    first_segment, first_offset, _ = covered[0]
    last_segment, _, last_offset = covered[-1]
    start = end = None
    cursor = 0
    for run in _content_runs(p_el):
        text, _ = _run_text(run)
        for segment in (first_segment, last_segment):
            if run is segment.run and text != segment.text:
                raise EditError(
                    "the writer and the extractor read the matched run "
                    "differently; apply this edit by hand"
                )
        if run is first_segment.run:
            start = cursor + first_offset
        if run is last_segment.run:
            end = cursor + last_offset
        cursor += len(text)
    if start is None or end is None or start >= end:
        raise EditError("the matched text did not align to any run boundary")
    return start, end


def _require_splittable_boundaries(p_el, start: int, end: int) -> None:
    """Refuse, before anything moves, a boundary ``_runs_covering`` would have
    to split a run at and ``_split_run`` could not.

    Equivalent to splitting and seeing: a boundary strictly inside a run needs
    that run split, and a run that can be split once leaves two runs that can
    be split again (one text node, nothing else), so the original runs decide.
    """
    cursor = 0
    for run in _content_runs(p_el):
        text, splittable = _run_text(run)
        run_end = cursor + len(text)
        if not splittable and any(cursor < boundary < run_end for boundary in (start, end)):
            raise EditError(_UNSPLITTABLE_REFUSAL)
        cursor = run_end


def _split_run(p_el, run_el, offset: int):
    """Split ``run_el`` at ``offset``; returns ``(left, right)`` run elements."""
    text, splittable = _run_text(run_el)
    if not splittable:
        raise EditError(_UNSPLITTABLE_REFUSAL)
    right = copy.deepcopy(run_el)
    _set_run_text(run_el, text[:offset])
    _set_run_text(right, text[offset:])
    run_el.addnext(right)
    return run_el, right


def _runs_covering(p_el, start: int, end: int) -> list:
    """Split as needed, then return the runs exactly covering ``[start, end)``.

    Splitting invalidates offsets, so the paragraph is re-scanned after each
    split rather than the indices being adjusted in place — the same
    correctness-over-cleverness trade the rest of this package makes.
    """
    # Align the start boundary.
    cursor = 0
    for run in _content_runs(p_el):
        text, _ = _run_text(run)
        run_end = cursor + len(text)
        if start < run_end:
            if start > cursor:
                _split_run(p_el, run, start - cursor)
            break
        cursor = run_end

    # Align the end boundary against the re-scanned paragraph.
    cursor = 0
    for run in _content_runs(p_el):
        text, _ = _run_text(run)
        run_end = cursor + len(text)
        if end < run_end:
            if end > cursor:
                _split_run(p_el, run, end - cursor)
            break
        cursor = run_end

    covered: list = []
    cursor = 0
    for run in _content_runs(p_el):
        text, _ = _run_text(run)
        run_end = cursor + len(text)
        if cursor >= start and run_end <= end and text:
            covered.append(run)
        cursor = run_end
    if not covered:
        raise EditError("the matched text did not align to any run boundary")
    return covered


# --------------------------------------------------------------------------
# Tracked-change elements
# --------------------------------------------------------------------------
def _revision_attrs(parent, revision_id: int, timestamp: str) -> dict:
    return {
        qn("w:id"): str(revision_id),
        qn("w:author"): REVISION_AUTHOR,
        qn("w:date"): timestamp,
    }


def _make(parent, tag: str, attrs: dict | None = None):
    element = parent.makeelement(qn(tag), attrs or {})
    return element


def _to_delete_text(run_el) -> None:
    """Convert a run's ``w:t`` nodes to ``w:delText`` for a tracked deletion."""
    for child in list(run_el):
        if child.tag == qn("w:t"):
            replacement = run_el.makeelement(qn("w:delText"), {})
            replacement.set(qn("xml:space"), "preserve")
            replacement.text = child.text
            run_el.replace(child, replacement)


def _run_properties(run_el):
    """A deep copy of a run's ``w:rPr``, or ``None``."""
    for child in run_el:
        if child.tag == qn("w:rPr"):
            return copy.deepcopy(child)
    return None


def _new_run(parent, text: str, properties=None):
    run = _make(parent, "w:r")
    if properties is not None:
        run.append(properties)
    _set_run_text(run, text)
    return run


@dataclass(frozen=True, eq=False)
class PlannedEdit:
    """One edit, decided against the unmutated document before anything is written.

    ``paragraph`` is the ``w:p`` an EDIT or DELETE changes, or the paragraph an
    ADD's new paragraph goes beside. For an EDIT or DELETE, ``span`` is the
    changed characters' ``[start, end)`` in that paragraph's visible text (the
    text the review read) and ``direct_span`` the same characters in the
    writer's run coordinates. For an ADD, ``gap`` is the insertion point as
    the two siblings it falls between — ``(anchor, next)`` after the anchor,
    ``(previous, anchor)`` before it, ``None`` at either end — so an addition
    after one paragraph and an addition before the next are seen to go to one
    place.

    Every element is held as a live reference: lxml hands back the same
    proxy for a node while one is alive, so the plans compare by identity,
    and references stay valid across sibling insertions.
    """

    entry: EditEntry
    paragraph: object
    span: tuple[int, int] | None = None
    direct_span: tuple[int, int] | None = None
    gap: tuple | None = None

    @property
    def is_addition(self) -> bool:
        return self.entry.action_type == ACTION_ADD


class DocumentEditor:
    """Applies located edits to one in-memory ``Document``.

    The editor is stateful only in its revision-id counter and its change
    log; it never reads or writes the filesystem.
    """

    def __init__(self, document, *, mode: EditMode = TRACKED) -> None:
        self.document = document
        self.mode = mode
        self._next_revision_id = _REVISION_ID_BASE
        self.timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _revision_id(self) -> int:
        value = self._next_revision_id
        self._next_revision_id += 1
        return value

    # -- id resolution ----------------------------------------------------
    def resolve_paragraphs(self, element_id: str, kind: ElementKind) -> list:
        """The candidate ``w:p`` elements an element id addresses.

        A body paragraph resolves to one; a table row resolves to every
        paragraph whose text the extractor put in the row, in the same order
        (a row's extracted text is its cells joined, so the specific paragraph
        is found by text among them). That includes paragraphs inside the
        cells' content controls and cells a control wraps: they are part of
        what the review saw, so they count when the writer checks whether the
        target is unique, though it never edits them.
        """
        if kind is ElementKind.BODY_PARAGRAPH:
            match = _BODY_PARAGRAPH_RE.match(element_id)
            if not match:
                raise EditError(f"malformed body paragraph id {element_id!r}")
            index = int(match.group(1))
            body = self.document.element.body
            children = list(body)
            if index >= len(children):
                raise EditError(
                    f"element {element_id} is past the end of this document "
                    f"({len(children)} body elements) — the document has "
                    "changed since the review"
                )
            element = children[index]
            if element.tag != qn("w:p"):
                raise EditError(
                    f"element {element_id} is no longer a paragraph in this "
                    "document — the document has changed since the review"
                )
            return [element]

        if kind is ElementKind.TABLE_ROW:
            return self._resolve_table_row(element_id)

        if kind is ElementKind.HEADER_FOOTER:
            match = _HEADER_FOOTER_RE.match(element_id)
            if not match:
                raise EditError(f"malformed header/footer id {element_id!r}")
            section_index, container_tag, para_index = (
                int(match.group(1)),
                match.group(2),
                int(match.group(3)),
            )
            sections = self.document.sections
            if section_index >= len(sections):
                raise EditError(f"section {section_index} does not exist")
            section = sections[section_index]
            container = section.header if container_tag == "h" else section.footer
            paragraphs = container.paragraphs
            if para_index >= len(paragraphs):
                raise EditError(
                    f"element {element_id} is past the end of its "
                    "header/footer"
                )
            return [paragraphs[para_index]._p]

        raise EditError(
            f"element {element_id} is in a container this applier does not "
            "write to"
        )

    def _resolve_table_row(self, element_id: str) -> list:
        match = _TABLE_PATH_RE.match(element_id)
        if not match:
            raise EditError(f"malformed table id {element_id!r}")
        table_index, row_index, nested_path = (
            int(match.group(1)),
            int(match.group(2)),
            match.group(3) or "",
        )
        tables = self.document.tables
        if table_index >= len(tables):
            raise EditError(f"table {table_index} does not exist in this document")
        table = tables[table_index]
        row, cells = self._row_cells(table, row_index, element_id)

        for cell_index, nested_table_index, nested_row_index in [
            (int(a), int(b), int(c)) for a, b, c in _NESTED_STEP_RE.findall(nested_path)
        ]:
            if cell_index >= len(cells):
                raise EditError(f"cell {cell_index} does not exist in {element_id}")
            nested_tables = cells[cell_index].tables
            if nested_table_index >= len(nested_tables):
                raise EditError(
                    f"nested table {nested_table_index} does not exist in "
                    f"{element_id}"
                )
            table = nested_tables[nested_table_index]
            row, cells = self._row_cells(table, nested_row_index, element_id)

        paragraphs: list = []
        for tc in _row_text_cells(row._tr, cells):
            paragraphs.extend(_cell_paragraphs(tc))
        return paragraphs

    @staticmethod
    def _row_cells(table: Table, row_index: int, element_id: str) -> tuple:
        """A row and its **distinct** cells, indexed as the extractor indexed them.

        The ``cN`` step of a nested-table id counts cells *after* the
        extractor's merge dedup (``_collect_table_mappings`` enumerates
        ``_unique_row_cells``), and that dedup is stateful across the whole
        table: python-docx returns a horizontally merged ``w:tc`` once per
        grid column it spans, and resolves a vertically merged cell's
        continuation rows to the origin ``w:tc`` in the row above. Indexing
        raw ``row.cells`` therefore drifts from the id on any merged table —
        `c1` would select a duplicate of the merged cell and the nested table
        would be reported missing.

        The extractor's own helper is used rather than a reimplementation,
        for the same reason the applier resolves ids with the extractor at
        all: two copies of merge semantics are two chances to disagree, and
        the disagreement lands an edit in the wrong cell. Rows 0..``row_index``
        are replayed so ``seen`` holds exactly what the extractor's did on
        arrival at this row, and the set is returned alive to the caller's
        frame because it owns the lxml proxies that keep ``tc`` identity
        stable.
        """
        rows = table.rows
        if row_index >= len(rows):
            raise EditError(
                f"row {row_index} does not exist in {element_id} — the table "
                "has changed since the review"
            )
        seen: set = set()
        cells: list = []
        for index in range(row_index + 1):
            cells = _unique_row_cells(rows[index], seen)
        return rows[row_index], cells

    # -- edit application -------------------------------------------------
    def resolve(self, location: Location) -> list:
        """The live paragraph elements a location addresses.

        Separate from :meth:`apply_resolved` because element ids are
        **positional** (``p7`` is the seventh child of ``w:body``), so the
        first ADD that inserts a paragraph renumbers every later id. A run
        therefore resolves every edit against the unmutated document first
        and applies afterwards, holding lxml element references, which stay
        valid across sibling insertions. Resolving lazily inside
        :meth:`apply` would silently shift later edits by one paragraph —
        the kind of bug that produces a plausible-looking document with an
        edit in the wrong clause.
        """
        if location.element_id is None:
            raise EditError("no element id to apply against")
        return self.resolve_paragraphs(location.element_id, location.kind)

    def plan(self, entry: EditEntry, paragraphs: list) -> PlannedEdit:
        """Decide exactly what ``entry`` changes, without changing anything.

        Raises every refusal this writer has — a target repeated within its
        element, text reached through a wrapper or another author's revision,
        a span across structure, a boundary inside a run that cannot be
        split — against the unmutated document. A run plans every edit before
        applying any, so it knows everything it will and will not do before
        the first write, and can tell which edits touch the same text.
        """
        if not paragraphs:
            raise EditError("the located element holds no paragraph to edit")
        if entry.action_type == ACTION_ADD:
            anchor = paragraphs[0]
            if entry.anchor_text:
                anchor, _, _ = self._target_paragraph(
                    paragraphs, entry.anchor_text, anchor=True
                )
            elif _inside_block_wrapper(anchor):
                raise EditError(_CONTENT_CONTROL_REFUSAL)
            if entry.insert_position == INSERT_BEFORE:
                gap = (anchor.getprevious(), anchor)
            else:
                gap = (anchor, anchor.getnext())
            return PlannedEdit(entry=entry, paragraph=anchor, gap=gap)
        p_el, span, direct = self._target_paragraph(paragraphs, entry.existing_text or "")
        _require_splittable_boundaries(p_el, *direct)
        return PlannedEdit(entry=entry, paragraph=p_el, span=span, direct_span=direct)

    def apply_planned(self, planned: PlannedEdit) -> str:
        """Write one planned edit, at the position it was planned at.

        Several plans for one paragraph stay valid when applied from the last
        span to the first: an edit changes the paragraph's runs only at and
        after its own start, so every span before it keeps its offsets (see
        ``applier.conflicts.application_order``). A new paragraph is inserted
        beside its planned anchor, whatever has since happened to the
        anchor's text.
        """
        if planned.is_addition:
            return self._insert_beside(planned.paragraph, planned.entry)
        return self._replace_span(planned.paragraph, *planned.direct_span, planned.entry)

    def apply_resolved(self, entry: EditEntry, paragraphs: list) -> str:
        """Plan and apply one entry against already-resolved elements."""
        return self.apply_planned(self.plan(entry, paragraphs))

    def apply(self, entry: EditEntry, location: Location) -> str:
        """Resolve, plan, and apply in one step. Safe for a single edit; a
        batch must :meth:`resolve` and :meth:`plan` every entry before the
        first :meth:`apply_planned`."""
        return self.apply_resolved(entry, self.resolve(location))

    def _target_paragraph(self, paragraphs: list, needle: str, *, anchor: bool = False):
        """The paragraph and span the edit applies to, or a refusal.

        Returns ``(paragraph, visible span, direct span)``; the direct span is
        ``None`` for an ADD's anchor.

        The target is matched against the extractor's visible text of each
        resolved paragraph — the text the review saw — never against the
        writer's own narrower view, so text inside a content control, field,
        smart tag, or hyperlink cannot be skipped over to a coincidental match
        in the runs around it.

        An element id names a paragraph or a table row — never *which
        occurrence inside it*. So when the target text appears more than once
        across the resolved elements, the sidecar does not say which one was
        meant, and taking the first would silently edit the wrong clause
        (irreversibly under ``--mode direct``). That is the same ambiguity
        the locator refuses one level up, and it is refused here for the same
        reason. A copy inside a wrapper counts: the finding may have meant it.

        The one match is then applicable only if its paragraph is not inside
        a block content control and every matched character comes from a
        plain run of that paragraph (plan WP-02: readable is not writable).
        For an EDIT or DELETE, nothing but position markers and
        formatting-only runs may sit between the matched runs either. An ADD
        (``anchor=True``) only positions a new paragraph beside the anchor's,
        so it needs the anchor located but not movable, and gets no span.
        """
        views = [(p_el, _paragraph_segments(p_el)) for p_el in paragraphs]
        occurrences = sum(
            count_occurrences(_visible_text(segments), needle) for _, segments in views
        )
        if occurrences > 1:
            raise EditError(
                f"the target text occurs {occurrences} times within the "
                "located element, and an element id does not say which "
                "occurrence was meant; apply this one by hand"
            )
        for p_el, segments in views:
            span = find_span(_visible_text(segments), needle)
            if span is None:
                continue
            if _inside_block_wrapper(p_el):
                raise EditError(_CONTENT_CONTROL_REFUSAL)
            covered = _covered_segments(segments, *span)
            _refuse_unwritable(covered)
            if anchor:
                return p_el, span, None
            runs = list(dict.fromkeys(segment.run for segment, _, _ in covered))
            _require_plain_span(p_el, runs)
            return p_el, span, _direct_span(p_el, covered)
        raise EditError(
            "the target text was not found in the located element (the "
            "document may have changed since the review, or the text may run "
            "across two paragraphs)"
        )

    def _replace_span(self, p_el, start: int, end: int, entry: EditEntry) -> str:
        """Replace or delete the direct-run characters ``[start, end)``."""
        covered = _runs_covering(p_el, start, end)
        removed = "".join(_run_text(run)[0] for run in covered)
        replacement = (
            entry.replacement_text if entry.action_type == ACTION_EDIT else None
        )

        if not self.mode.tracked:
            first = covered[0]
            _set_run_text(first, replacement or "")
            for run in covered[1:]:
                p_el.remove(run)
            if replacement is None:
                p_el.remove(first)
            action = "replaced" if replacement is not None else "deleted"
            return f"{action} {removed[:80]!r} in {entry.section or 'the document'}"

        properties = _run_properties(covered[0])
        deletion = _make(p_el, "w:del", _revision_attrs(p_el, self._revision_id(), self.timestamp))
        covered[0].addprevious(deletion)
        for run in covered:
            p_el.remove(run)
            _to_delete_text(run)
            deletion.append(run)

        if replacement is not None:
            insertion = _make(
                p_el, "w:ins", _revision_attrs(p_el, self._revision_id(), self.timestamp)
            )
            insertion.append(_new_run(p_el, replacement, properties))
            deletion.addnext(insertion)
            return (
                f"tracked replacement of {removed[:80]!r} with "
                f"{replacement[:80]!r}"
            )
        return f"tracked deletion of {removed[:80]!r}"

    def _insert_beside(self, anchor, entry: EditEntry) -> str:
        """Insert ``entry``'s new paragraph before or after ``anchor``."""
        new_paragraph = self._build_paragraph_like(anchor, entry.replacement_text or "")
        if entry.insert_position == INSERT_BEFORE:
            anchor.addprevious(new_paragraph)
            where = "before"
        else:
            anchor.addnext(new_paragraph)
            where = "after"
        kind = "tracked insertion" if self.mode.tracked else "insertion"
        return (
            f"{kind} of a new paragraph {where} the anchor: "
            f"{(entry.replacement_text or '')[:80]!r}"
        )

    def _build_paragraph_like(self, anchor_p_el, text: str):
        """A new paragraph inheriting the anchor's style and numbering.

        Spec paragraphs carry their article numbering in ``w:pPr``; a new
        paragraph built from scratch would land unnumbered in the middle of a
        numbered list, which reads as a formatting bug rather than a proposed
        requirement.
        """
        paragraph = copy.deepcopy(anchor_p_el)
        for child in list(paragraph):
            if child.tag != qn("w:pPr"):
                paragraph.remove(child)

        properties = None
        for run in _content_runs(anchor_p_el):
            properties = _run_properties(run)
            if properties is not None:
                break

        if self.mode.tracked:
            self._mark_paragraph_inserted(paragraph)
            insertion = _make(
                paragraph,
                "w:ins",
                _revision_attrs(paragraph, self._revision_id(), self.timestamp),
            )
            insertion.append(_new_run(paragraph, text, properties))
            paragraph.append(insertion)
        else:
            paragraph.append(_new_run(paragraph, text, properties))
        return paragraph

    def _mark_paragraph_inserted(self, paragraph) -> None:
        """Mark the new paragraph's own mark as inserted.

        Without this, rejecting the insertion in Word removes the text but
        leaves an empty numbered paragraph behind — a reject that does not
        restore the original is not a reject.
        """
        properties = None
        for child in paragraph:
            if child.tag == qn("w:pPr"):
                properties = child
                break
        if properties is None:
            properties = _make(paragraph, "w:pPr")
            paragraph.insert(0, properties)
        run_properties = None
        for child in properties:
            if child.tag == qn("w:rPr"):
                run_properties = child
                break
        if run_properties is None:
            run_properties = _make(properties, "w:rPr")
            properties.append(run_properties)
        for child in list(run_properties):
            if child.tag == qn("w:ins"):
                run_properties.remove(child)
        run_properties.insert(
            0,
            _make(
                run_properties,
                "w:ins",
                _revision_attrs(
                    run_properties, self._revision_id(), self.timestamp
                ),
            ),
        )
