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
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from docx.oxml.ns import qn
from docx.table import Table

from src.input.extractor import _unique_row_cells

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
def _run_text(run_el) -> tuple[str, bool]:
    """``(visible_text, splittable)`` for a ``w:r`` element.

    ``splittable`` is False when the run carries anything this module cannot
    reconstruct after a split — a tab, a break, a non-breaking hyphen, or
    more than one text node.
    """
    parts: list[str] = []
    text_nodes = 0
    splittable = True
    for child in run_el:
        tag = child.tag
        if tag == qn("w:t"):
            parts.append(child.text or "")
            text_nodes += 1
        elif tag == qn("w:tab"):
            parts.append("\t")
            splittable = False
        elif tag in (qn("w:br"), qn("w:cr")):
            parts.append("\n")
            splittable = False
        elif tag == qn("w:noBreakHyphen"):
            parts.append("-")
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


def _text_including_revisions(p_el) -> str:
    """Every run's text, including runs nested inside ``w:ins`` / ``w:del``.

    Used only to explain a refusal. This module edits *direct* runs, so text
    that exists only inside someone else's pending revision is unreachable —
    and saying "not found" about text a reviewer can plainly see in Word
    would send them hunting for the wrong problem.
    """
    return "".join(
        _run_text(run)[0] for run in p_el.iter(qn("w:r"))
    )


def _split_run(p_el, run_el, offset: int):
    """Split ``run_el`` at ``offset``; returns ``(left, right)`` run elements."""
    text, splittable = _run_text(run_el)
    if not splittable:
        raise EditError(
            "the edit boundary falls inside a run carrying a tab, a line "
            "break, or multiple text nodes; apply this one by hand so its "
            "layout is preserved"
        )
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
        paragraph in its cells (a row's extracted text is its cells joined,
        so the specific paragraph is found by text among them).
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
        cells = self._row_cells(table, row_index, element_id)

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
            cells = self._row_cells(table, nested_row_index, element_id)

        paragraphs: list = []
        for cell in cells:
            paragraphs.extend(paragraph._p for paragraph in cell.paragraphs)
        return paragraphs

    @staticmethod
    def _row_cells(table: Table, row_index: int, element_id: str) -> list:
        """A row's **distinct** cells, indexed as the extractor indexed them.

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
        return cells

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

    def apply_resolved(self, entry: EditEntry, paragraphs: list) -> str:
        """Apply one entry against already-resolved elements."""
        if entry.action_type == ACTION_ADD:
            return self._apply_add(entry, paragraphs)
        return self._apply_inline(entry, paragraphs)

    def apply(self, entry: EditEntry, location: Location) -> str:
        """Resolve and apply in one step. Safe for a single edit; a batch
        must use :meth:`resolve` for every entry before the first
        :meth:`apply_resolved`."""
        return self.apply_resolved(entry, self.resolve(location))

    def _target_paragraph(self, paragraphs: list, needle: str):
        """The paragraph and span the edit applies to, or a refusal.

        An element id names a paragraph or a table row — never *which
        occurrence inside it*. So when the target text appears more than once
        across the resolved elements, the sidecar does not say which one was
        meant, and taking the first would silently edit the wrong clause
        (irreversibly under ``--mode direct``). That is the same ambiguity
        the locator refuses one level up, and it is refused here for the same
        reason.
        """
        occurrences = sum(
            count_occurrences(_paragraph_text(p_el), needle) for p_el in paragraphs
        )
        if occurrences > 1:
            raise EditError(
                f"the target text occurs {occurrences} times within the "
                "located element, and an element id does not say which "
                "occurrence was meant; apply this one by hand"
            )
        for p_el in paragraphs:
            span = find_span(_paragraph_text(p_el), needle)
            if span is not None:
                return p_el, span
        if any(
            find_span(_text_including_revisions(p_el), needle) is not None
            for p_el in paragraphs
        ):
            raise EditError(
                "the target text sits inside an existing tracked revision by "
                "another author. Accept or reject that revision in Word "
                "first — editing inside an undecided change would leave the "
                "document's history unreadable"
            )
        raise EditError(
            "the target text was not found in the located element's runs "
            "(it may be split across a hyperlink or a field code)"
        )

    def _apply_inline(self, entry: EditEntry, paragraphs: list) -> str:
        needle = entry.existing_text or ""
        p_el, (start, end) = self._target_paragraph(paragraphs, needle)
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

    def _apply_add(self, entry: EditEntry, paragraphs: list) -> str:
        anchor = paragraphs[0]
        if entry.anchor_text:
            anchor, _ = self._target_paragraph(paragraphs, entry.anchor_text)
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
