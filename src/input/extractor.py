"""Text extraction for Spec Critic specs (DOCX) and Project Context
attachments (DOCX / PDF / Markdown / plain text)."""

from pathlib import Path
from dataclasses import dataclass, field
from typing import NamedTuple
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml import parse_xml
from docx.oxml.ns import qn
from docx.table import Table as DocxTable

SUPPORTED_EXTENSIONS = {".docx"}

# Project-context attachments are reviewed as background reference material
# (not edited by the spec pipeline), so several read-only formats are accepted
# in addition to DOCX: PDFs, and plain Markdown / text. The Markdown / text
# path lets reviewers attach external reference notes — for example a
# drawing-set digest produced by a separate analysis tool — as Project Context.
CONTEXT_ATTACHMENT_EXTENSIONS = {".docx", ".pdf", ".md", ".txt"}


@dataclass
class ParagraphMapping:
    body_index: int
    element_type: str
    text: str
    table_index: int | None
    row_index: int | None
    cell_index: int | None
    section_index: int | None = None
    container_type: str | None = None
    # Stable, deterministic element identifier scoped to a single
    # extracted document. The format is human-readable so a finding that
    # cites it can be debugged at a glance: ``p<body_index>`` for body
    # paragraphs, ``t<table>r<row>`` for table-cell rows (a row of a table
    # nested inside a cell extends that path — ``t<table>r<row>c<cell>t<nested>r<row>``,
    # see ``_collect_table_mappings``), ``s<n>h<i>`` /
    # ``s<n>f<i>`` for section header / footer paragraphs, ``tb<box>p<para>``
    # for text-box paragraphs, ``fn<id>p<para>`` / ``en<id>p<para>`` for
    # footnote / endnote paragraphs, and ``meta:hf`` / ``meta:tb`` /
    # ``meta:fn`` / ``meta:en`` for the synthetic delimiter that precedes
    # each supplemental block. Text read from inside a block content control
    # (plan WP-02) has ids of its own, each with a ``cc<n>`` step, so the
    # ids above keep their meaning: ``cc<body_index>p<i>`` / ``cc<body_index>t<i>r<row>``
    # for a paragraph / table row inside the control at that body index
    # (``i`` the element's index in the control's content; a nested control
    # adds ``cc<i>``), ``<table path>cc<k>r<i>`` for a row a control wraps
    # inside a table, and ``s<n>hcc<k>p<i>`` / ``tb<box>cc<k>p<i>`` /
    # ``fn<id>cc<k>p<i>`` for a control's paragraph in a header, footer,
    # text box, or note. The id is stable within a single extraction
    # run; for cross-run stability the document_id of the owning
    # ``ExtractedSpec`` should also be checked. Empty string for legacy
    # mappings constructed by tests that predate element ids.
    element_id: str = ""
    # Section heading text the element belongs to (best-effort). Surfacing
    # this lets a downstream applier disambiguate identical text in
    # different sections without re-scanning the paragraph map.
    section_id: str = ""


@dataclass
class ExtractedSpec:
    filename: str
    content: str
    word_count: int
    source_path: str = ""
    source_format: str = ""
    paragraph_map: list[ParagraphMapping] | None = None
    # Stable, human-debuggable document identifier. Defaults to
    # the filename without extension; when filenames could collide the
    # caller can override. Element ids are only unique inside a single
    # document, so a downstream applier pairs ``(document_id, element_id)``
    # when it disambiguates findings that cite an id.
    document_id: str = ""
    # Warnings emitted during text extraction
    # that the report banner surfaces so reviewers can spot specs where
    # text content may not have been fully captured (drawing-heavy
    # documents, embedded objects, etc.). Empty list by default; populated
    # only when the extractor's heuristics fire. Listed per spec — the
    # run-diagnostics banner counts the number of specs with any warnings,
    # not the total warning count, so a single spec with multiple
    # warnings still counts as one affected file.
    extraction_warnings: list[str] = field(default_factory=list)
    # True when the source document contained pending Word "Track Changes"
    # (revision) markup at extraction time. The extracted ``content`` is the
    # Accept-All-Changes view (insertions kept, deletions removed); this flag
    # lets the report advise reviewers that the spec was read as accept-all so
    # they can confirm that is the version they meant to review. Detected across
    # every surface the extractor reads — the body (incl. tables and text
    # boxes), section headers/footers, and footnote/endnote parts — so the
    # advisory fires even when a redline is confined to a header/footer or note.
    # Defaults False (the common case).
    tracked_changes_detected: bool = False


def _derive_document_id(filename: str) -> str:
    """Return a stable, human-readable document id for ``filename``.

    Ids stay debuggable: the filename without its extension is
    enough as a per-run identifier and reads cleanly in logs. Callers that
    expect cross-run stability across renames should override
    ``ExtractedSpec.document_id`` themselves.
    """
    if not filename:
        return ""
    return Path(filename).stem or filename


def _is_heading_paragraph(text: str) -> bool:
    """Heuristic match for a CSI / DSA spec heading paragraph.

    A cheap, deterministic section attribution lets the
    paragraph map can carry a ``section_id`` without re-walking the doc.
    The heuristic only has to be close enough that downstream prompts and
    reports can group paragraphs by section. False positives are harmless
    — they shift the section boundary by one paragraph.
    """
    stripped = (text or "").strip()
    if not stripped or len(stripped) > 80:
        return False
    # "PART 1 GENERAL" / "SECTION 23 05 23" — explicit headings.
    upper = stripped.upper()
    if upper.startswith("PART ") or upper.startswith("SECTION "):
        return True
    # "1.01 SUMMARY" / "2.3.A …" — numbered CSI subheadings.
    first_token = stripped.split(maxsplit=1)[0]
    if first_token and first_token[0].isdigit() and any(
        ch == "." for ch in first_token
    ):
        return True
    return False


# Threshold above which a spec is flagged as
# drawing-heavy. The pipeline writes the raw count and the proportion into
# the warning message so reviewers can see why the spec was flagged
# (drawings, embedded pictures, OLE objects). 20% is conservative; a
# typical drawing-supplemented spec carries figures inline at ~10% of body
# elements. Above that proportion the assumption that text extraction
# captures the reviewable content stops holding and the warning prompts
# a manual visual check.
_CONTENT_LOSS_WARNING_THRESHOLD = 0.20


def _detect_content_loss_warning(body) -> str | None:
    """Return a content-loss warning string for ``body`` or ``None`` if clean.

    Counts how many direct children of
    ``<w:body>`` (paragraphs and tables) contain at least one descendant
    ``<w:drawing>``, ``<w:pict>``, or ``<w:object>`` element. When that
    proportion exceeds :data:`_CONTENT_LOSS_WARNING_THRESHOLD`, the spec
    is likely drawing-heavy and text-only extraction cannot capture the
    reviewable content. The returned string is appended to
    ``ExtractedSpec.extraction_warnings`` so the report banner surfaces a
    visible count (per-spec, not per-drawing) and the operator knows to
    verify the spec visually.

    The ``<w:sectPr>`` body child (section properties) is metadata and
    not counted as a content element. Returns ``None`` when there are no
    body children, no embedded objects, or the proportion is below the
    threshold so the caller can keep ``extraction_warnings`` empty for
    the common case.
    """
    drawing_qn = qn("w:drawing")
    pict_qn = qn("w:pict")
    object_qn = qn("w:object")
    sect_pr_qn = qn("w:sectPr")

    total_body_elements = 0
    non_text_elements = 0
    drawings = 0
    pictures = 0
    objects = 0
    for child in body:
        if child.tag == sect_pr_qn:
            continue
        total_body_elements += 1
        child_drawings = len(child.findall(".//" + drawing_qn))
        child_pictures = len(child.findall(".//" + pict_qn))
        child_objects = len(child.findall(".//" + object_qn))
        if child_drawings or child_pictures or child_objects:
            non_text_elements += 1
        drawings += child_drawings
        pictures += child_pictures
        objects += child_objects

    if total_body_elements == 0:
        return None
    if non_text_elements == 0:
        return None
    proportion = non_text_elements / total_body_elements
    if proportion <= _CONTENT_LOSS_WARNING_THRESHOLD:
        return None

    percent = round(proportion * 100)
    return (
        f"Spec contains {percent}% non-text elements "
        f"({drawings} drawings, {pictures} pictures, {objects} OLE objects). "
        "Some content may not have been extracted for review. Verify visually."
    )


# Footnotes and endnotes live in their own package parts (``word/footnotes.xml``
# / ``word/endnotes.xml``), not under ``<w:body>``, so the body walk never
# reaches them. The parts are identified by their OOXML content type. Word
# seeds every document that has the part with structural notes (``separator``
# / ``continuationSeparator``) that carry no authored text; those are skipped
# by their ``w:type`` so an empty ``[Footnote -1]`` label never reaches review.
_FOOTNOTES_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
)
_ENDNOTES_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml"
)
_STRUCTURAL_NOTE_TYPES = {"separator", "continuationSeparator", "continuationNotice"}


def _in_unread_alternative(element) -> bool:
    """Whether ``element`` sits in a markup-compatibility branch a reader skips.

    Word saves every text box twice, inside ``<mc:AlternateContent>``: a
    DrawingML shape in ``<mc:Choice>`` and a VML copy in ``<mc:Fallback>``,
    each holding the same ``<w:txbxContent>``. A consumer reads exactly one
    branch of each alternate. The branch read here is the first one (in
    document order) that holds a text box, which for Word's own output is the
    DrawingML choice; the others are copies of it.
    """
    for ancestor in element.iterancestors():
        if ancestor.tag not in _MC_BRANCHES:
            continue
        alternate = ancestor.getparent()
        if alternate is None or alternate.tag != _MC_ALTERNATE_CONTENT:
            continue
        chosen = next(
            (
                branch
                for branch in alternate
                if branch.tag in _MC_BRANCHES
                and branch.find(".//" + _W_TXBX_CONTENT) is not None
            ),
            None,
        )
        if chosen is not ancestor:
            return True
    return False


def _text_boxes(body) -> list:
    """Every text box in the body, in document order, each read once (see
    :func:`_in_unread_alternative`)."""
    return [box for box in body.iter(_W_TXBX_CONTENT) if not _in_unread_alternative(box)]


def _collect_textbox_mappings(body, unsupported=None) -> list[ParagraphMapping]:
    """Extract text authored inside drawing / VML text boxes.

    Text-box text is stored in ``<w:txbxContent>`` elements nested inside
    ``<w:drawing>`` (modern DrawingML) or ``<w:pict>`` (legacy VML) runs.
    ``Paragraph.text`` does not descend into them, so the plain body walk
    silently drops a requirement authored in a callout / sidebar text box —
    a "miss a real problem" gap (TRUST_AUDIT P0-6). This collects every text
    box in document order and emits one mapping per non-empty text-box
    paragraph. A nested text box is reached by the same descendant search
    and its parent's ``Paragraph.text`` does not include it, so each box is
    captured exactly once (no duplication, no miss) — and the VML copy Word
    saves beside each DrawingML text box is not read a second time
    (:func:`_text_boxes`). A block content control inside a text box is read
    too, under ``tb<box>cc<k>p<i>`` (:func:`_story_paragraphs`).
    """
    mappings: list[ParagraphMapping] = []
    for box_index, txbx in enumerate(_text_boxes(body)):
        for ordinal, wrapped_path, para_el in _story_paragraphs(txbx, unsupported):
            text = _accept_all_paragraph_text(para_el, unsupported).strip()
            if not text:
                continue
            if wrapped_path is None:
                element_id = f"tb{box_index}p{ordinal}"
                container_type = "textbox"
            else:
                element_id = f"tb{box_index}{wrapped_path}"
                container_type = CONTENT_CONTROL_CONTAINER
            mappings.append(
                ParagraphMapping(
                    body_index=-1,
                    element_type="textbox",
                    text=f"[Text Box] {text}",
                    table_index=None,
                    row_index=None,
                    cell_index=None,
                    container_type=container_type,
                    element_id=element_id,
                    section_id="",
                )
            )
    return mappings


def _find_part_by_content_type(doc_part, content_type: str):
    """Return the related package part of ``content_type`` (or ``None``).

    Relationship ids are not stable across authoring tools, so footnote /
    endnote parts are located by their OOXML content type. Shared by the
    note-extraction path and the tracked-change detector.
    """
    for rel in doc_part.rels.values():
        if rel.is_external:
            continue
        target = rel.target_part
        if getattr(target, "content_type", None) == content_type:
            return target
    return None


def _collect_note_mappings(
    doc_part,
    *,
    content_type: str,
    note_tag: str,
    label: str,
    id_prefix: str,
    unsupported=None,
) -> list[ParagraphMapping]:
    """Extract footnote / endnote text from the package part of ``content_type``.

    Footnotes and endnotes are not under ``<w:body>``; they hang off the
    document part by relationship. The part is located by content type
    (relationship ids are not stable), parsed defensively, and walked for
    ``<w:footnote>`` / ``<w:endnote>`` elements. Structural notes
    (``separator`` etc.) are skipped by ``w:type``. Returns one mapping per
    non-empty note paragraph, or an empty list when the part is absent (the
    common case) or unreadable — body text is the primary deliverable and a
    malformed notes part must never sink the whole extraction. A block
    content control inside a note is read too, under ``fn<id>cc<k>p<i>``
    (:func:`_story_paragraphs`).
    """
    note_part = _find_part_by_content_type(doc_part, content_type)
    if note_part is None:
        return []
    try:
        root = parse_xml(note_part.blob)
    except Exception:
        return []

    element_type = label.lower()
    w_id = qn("w:id")
    w_type = qn("w:type")
    if unsupported is not None:
        unsupported.alt_chunks.update(root.iter(_W_ALT_CHUNK))
    mappings: list[ParagraphMapping] = []
    for note in root.findall(qn(note_tag)):
        if note.get(w_type) in _STRUCTURAL_NOTE_TYPES:
            continue
        note_id = note.get(w_id) or "?"
        for ordinal, wrapped_path, para_el in _story_paragraphs(note, unsupported):
            text = _accept_all_paragraph_text(para_el, unsupported).strip()
            if not text:
                continue
            if wrapped_path is None:
                element_id = f"{id_prefix}{note_id}p{ordinal}"
                container_type = element_type
            else:
                element_id = f"{id_prefix}{note_id}{wrapped_path}"
                container_type = CONTENT_CONTROL_CONTAINER
            mappings.append(
                ParagraphMapping(
                    body_index=-1,
                    element_type=element_type,
                    text=f"[{label} {note_id}] {text}",
                    table_index=None,
                    row_index=None,
                    cell_index=None,
                    container_type=container_type,
                    element_id=element_id,
                    section_id="",
                )
            )
    return mappings


def _append_supplemental_block(
    paragraphs: list[str],
    paragraph_map: list[ParagraphMapping],
    *,
    delimiter: str,
    delimiter_id: str,
    container_type: str,
    entries: list[ParagraphMapping],
) -> None:
    """Append a labeled block (delimiter + its entries) to both the flat text
    list and the paragraph map, in lockstep.

    Supplemental content (text boxes, footnotes, endnotes, headers/footers)
    does not flow inline in ``<w:body>``, so each kind is rendered as its own
    labeled block after the body. Appending to ``paragraphs`` and
    ``paragraph_map`` together preserves the reconstruction invariant (the
    map's text must join back to ``content``). A no-op when ``entries`` is
    empty, so a spec with none of a given kind produces byte-identical output.
    """
    if not entries:
        return
    paragraph_map.append(
        ParagraphMapping(
            body_index=-1,
            element_type="meta",
            text=delimiter,
            table_index=None,
            row_index=None,
            cell_index=None,
            container_type=container_type,
            element_id=delimiter_id,
            section_id="",
        )
    )
    paragraphs.append(delimiter)
    paragraph_map.extend(entries)
    paragraphs.extend(entry.text for entry in entries)


# ---------------------------------------------------------------------------
# Visible text: tracked changes, wrappers, and fields (plan WP-02)
# ---------------------------------------------------------------------------
#
# Tracked changes. When a reviewer leaves Word's "Track Changes" on, edits are
# stored as revision markup rather than applied to the text:
#   * <w:ins> wraps inserted runs (kept when changes are accepted),
#   * <w:del> wraps deleted runs whose text lives in <w:delText> (removed),
#   * <w:moveTo> / <w:moveFrom> wrap the destination / source of a move.
# python-docx's ``Paragraph.text`` selects only direct-child <w:r>/<w:hyperlink>
# runs and reads only <w:t> (never <w:delText>), so it silently drops BOTH
# inserted text (nested under <w:ins>) and deleted text — a hybrid that matches
# neither Word's "Accept All Changes" nor "Reject All Changes" view, and a
# combined edit (delete "2019", insert "2025") collapses to "Comply with  CBC.".
# We instead reconstruct the Accept-All view: keep insertions and move
# destinations, drop deletions and move sources. That is the text that will
# remain once the redline is accepted — i.e. what will actually be issued —
# computed in memory without modifying the source file.
#
# Wrappers. Visible text is not always a run of its paragraph. Inside a
# paragraph it can sit in a content control (<w:sdt>: its <w:sdtContent> holds
# what Word shows — a typed value, the chosen drop-down entry, or an unfilled
# control's placeholder), a smart tag (<w:smartTag>), a custom XML element
# (<w:customXml>), a simple field (<w:fldSimple>, whose runs are the field's
# stored result), a hyperlink (which can hold revisions of its own), or a
# bidirectional-text container (<w:dir> / <w:bdo>). ``Paragraph.text`` reads
# none of them but a hyperlink's direct runs. Outside paragraphs, a block
# content control (or custom XML block) can wrap whole paragraphs and tables,
# table rows, or table cells. The walk descends through each of these
# structurally — never by a catch-all descendant-text search — and applies the
# Accept-All rules at every depth.
#
# Fields. A complex field is a run sequence: <w:fldChar begin>, the instruction
# (<w:instrText>, possibly holding nested fields), <w:fldChar separate>, the
# stored result, <w:fldChar end>. Only the stored result is visible. The walk
# follows the field state within the paragraph, so an instruction is never
# read as prose — including a nested field's result, which sits inside the
# outer instruction as an argument, not as display text. A simple field's
# instruction is an attribute (w:instr) and is never read. Nothing is executed
# or updated: a stored result is read as it was stored.
_W_P = qn("w:p")
_W_R = qn("w:r")
_W_TBL = qn("w:tbl")
_W_TR = qn("w:tr")
_W_TC = qn("w:tc")
_W_HYPERLINK = qn("w:hyperlink")
_W_INS = qn("w:ins")
_W_DEL = qn("w:del")
_W_MOVE_FROM = qn("w:moveFrom")
_W_MOVE_TO = qn("w:moveTo")
_W_SDT = qn("w:sdt")
_W_SDT_PR = qn("w:sdtPr")
_W_SDT_CONTENT = qn("w:sdtContent")
_W_SMART_TAG = qn("w:smartTag")
_W_CUSTOM_XML = qn("w:customXml")
_W_CUSTOM_XML_PR = qn("w:customXmlPr")
_W_FLD_SIMPLE = qn("w:fldSimple")
_W_DIR = qn("w:dir")
_W_BDO = qn("w:bdo")
_W_FLD_CHAR = qn("w:fldChar")
_W_FLD_CHAR_TYPE = qn("w:fldCharType")
_W_FF_DATA = qn("w:ffData")
_W_DD_LIST = qn("w:ddList")
_W_ALT_CHUNK = qn("w:altChunk")
_W_DOC_PART_OBJ = qn("w:docPartObj")
_W_DOC_PART_GALLERY = qn("w:docPartGallery")
_W_VAL = qn("w:val")
_W_TXBX_CONTENT = qn("w:txbxContent")

_MATH_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_MATH_TAGS = frozenset({f"{{{_MATH_NS}}}oMath", f"{{{_MATH_NS}}}oMathPara"})
_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_MC_ALTERNATE_CONTENT = f"{{{_MC_NS}}}AlternateContent"
_MC_BRANCHES = frozenset({f"{{{_MC_NS}}}Choice", f"{{{_MC_NS}}}Fallback"})

# Revision wrappers whose content survives "Accept All Changes" — the walk
# descends through these to reach the runs they wrap. The value is the label a
# segment's route records for text inside one.
_ACCEPTED_REVISION_WRAPPERS = {_W_INS: "ins", _W_MOVE_TO: "moveTo"}

# Inline containers whose children are visible run content, with the label a
# segment's route records. <w:sdt> is handled apart: only its <w:sdtContent>
# is content (<w:sdtPr> holds the control's properties, list items included).
# The containers' own property children (<w:smartTagPr>, <w:customXmlPr>,
# <w:fldData>) are not runs, so the walk never reads them.
_INLINE_WRAPPERS = {
    _W_HYPERLINK: "hyperlink",
    _W_SMART_TAG: "smartTag",
    _W_CUSTOM_XML: "customXml",
    _W_FLD_SIMPLE: "fldSimple",
    _W_DIR: "dir",
    _W_BDO: "bdo",
}

#: Route label for text that is part of a complex field's stored result. It
#: leads the route because the field encloses whatever containers the result
#: sits in (a table of contents' hyperlinks, for instance).
FIELD_RESULT_ROUTE = "field"

#: Route label for text inside an inline content control.
CONTENT_CONTROL_ROUTE = "sdt"

# Block-level wrappers: a content control or custom XML element around whole
# paragraphs and tables (or, inside a table, around rows or cells).
_BLOCK_WRAPPERS = frozenset({_W_SDT, _W_CUSTOM_XML})

#: ``ParagraphMapping.container_type`` of every element read from inside a
#: block wrapper — exactly the elements whose id carries a ``cc<n>`` step.
CONTENT_CONTROL_CONTAINER = "content_control"

# The run content python-docx translates to text (``CT_R.text``): a text node,
# a tab, a line break, a carriage return, a non-breaking hyphen, a positional
# tab. ``str()`` of each is its text equivalent.
_RUN_TEXT_TAGS = frozenset(
    qn(tag) for tag in ("w:br", "w:cr", "w:noBreakHyphen", "w:ptab", "w:t", "w:tab")
)

# Any of these anywhere in the document means a reviewer left tracked changes
# pending (used only for the report advisory, not for text extraction).
_REVISION_MARKER_TAGS = (_W_INS, _W_DEL, _W_MOVE_FROM, _W_MOVE_TO)


class _Segment(NamedTuple):
    """One piece of a paragraph's visible text, and where it came from.

    ``run`` is the ``<w:r>`` the text belongs to. ``route`` names every
    container between the paragraph and that run, outermost first — ``()``
    for a plain run that is a direct child of the paragraph — with
    :data:`FIELD_RESULT_ROUTE` leading it when the run is part of a complex
    field's stored result. The applier reads routes to refuse an edit whose
    text it cannot write safely; because it uses this same walk, the text it
    matches is the text the review saw, character for character.
    """

    text: str
    run: object
    route: tuple


class _FieldState:
    """Complex-field nesting within one paragraph.

    Each open field is ``False`` while its instruction is being read and
    ``True`` once its ``separate`` character starts the stored result. Text is
    visible only while every open field is in its result. A ``separate`` or
    ``end`` with no open field (a field that began in an earlier paragraph,
    such as a multi-paragraph table of contents) changes nothing, so the rest
    of the paragraph reads normally.
    """

    __slots__ = ("_open",)

    def __init__(self) -> None:
        self._open: list[bool] = []

    def mark(self, fld_char) -> None:
        kind = fld_char.get(_W_FLD_CHAR_TYPE)
        if kind == "begin":
            self._open.append(False)
        elif kind == "separate":
            if self._open:
                self._open[-1] = True
        elif kind == "end":
            if self._open:
                self._open.pop()

    @property
    def visible(self) -> bool:
        return all(self._open)

    @property
    def in_result(self) -> bool:
        return bool(self._open)


def _count(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


@dataclass
class _Unsupported:
    """Text-bearing structures the walk meets but does not read (plan WP-02 item 5).

    Each set holds the elements themselves (identity, so a header read twice
    for two linked sections still counts its equation once). The warnings
    state what was found and how many, never how much text that is: the
    extractor does not know, and a completeness figure it cannot prove would
    be invented.
    """

    equations: set = field(default_factory=set)
    alt_chunks: set = field(default_factory=set)
    legacy_drop_downs: set = field(default_factory=set)
    table_control_tables: set = field(default_factory=set)

    def warnings(self) -> list[str]:
        found: list[str] = []
        if self.equations:
            found.append(
                f"Spec contains {_count(len(self.equations), 'equation', 'equations')} "
                "(Office Math) whose text was not extracted for review. Verify visually."
            )
        if self.alt_chunks:
            found.append(
                "Spec contains "
                f"{_count(len(self.alt_chunks), 'embedded document', 'embedded documents')} "
                "(altChunk) whose text was not extracted for review. Verify visually."
            )
        if self.legacy_drop_downs:
            # Word records a legacy drop-down's choice as a position in the
            # field's list, not as text in the paragraph.
            count = len(self.legacy_drop_downs)
            found.append(
                "Spec contains "
                + _count(count, "legacy drop-down form field", "legacy drop-down form fields")
                + (
                    " whose chosen entry was"
                    if count == 1
                    else " whose chosen entries were"
                )
                + " not extracted for review. Verify visually."
            )
        if self.table_control_tables:
            found.append(
                f"Spec contains {_count(len(self.table_control_tables), 'table', 'tables')} "
                "inside content controls within table rows or cells; their text was "
                "not extracted for review. Verify visually."
            )
        return found


def _run_segments(run, route: tuple, fields: _FieldState, out: list, unsupported) -> None:
    """Append the visible text of one ``<w:r>`` to ``out`` as segments.

    A run with no field characters is read with python-docx's own
    ``CT_R.text`` (so a paragraph with no revision, wrapper, or field markup
    reads byte-identically to ``Paragraph.text``). A run carrying field
    characters is read child by child so the field state changes exactly
    where they sit. ``<w:instrText>`` and ``<w:delText>`` are never text.
    """
    if run.find(_W_FLD_CHAR) is None:
        if fields.visible:
            text = run.text
            if text:
                label = ((FIELD_RESULT_ROUTE,) + route) if fields.in_result else route
                out.append(_Segment(text, run, label))
        return

    buffered: list[str] = []

    def flush() -> None:
        if buffered:
            label = ((FIELD_RESULT_ROUTE,) + route) if fields.in_result else route
            out.append(_Segment("".join(buffered), run, label))
            buffered.clear()

    for child in run:
        tag = child.tag
        if tag == _W_FLD_CHAR:
            flush()
            if (
                unsupported is not None
                and child.get(_W_FLD_CHAR_TYPE) == "begin"
                and child.find(f"{_W_FF_DATA}/{_W_DD_LIST}") is not None
            ):
                unsupported.legacy_drop_downs.add(child)
            fields.mark(child)
        elif tag in _RUN_TEXT_TAGS and fields.visible:
            text = str(child)
            if text:
                buffered.append(text)
    flush()


def _walk_inline(container, route: tuple, fields: _FieldState, out: list, unsupported) -> None:
    """Append the visible segments under ``container`` (a paragraph or an
    inline wrapper) in document order.

    Descends through accepted revisions (``<w:ins>`` / ``<w:moveTo>``) and
    every inline wrapper, skips ``<w:del>`` / ``<w:moveFrom>`` entirely (they
    disappear on accept), and ignores everything else (paragraph properties,
    bookmarks, proofing marks, ...). An equation is visible but not read, and
    is recorded in ``unsupported`` when the caller keeps one.
    """
    for child in container:
        tag = child.tag
        if tag == _W_R:
            _run_segments(child, route, fields, out, unsupported)
        elif tag in _ACCEPTED_REVISION_WRAPPERS:
            _walk_inline(
                child, route + (_ACCEPTED_REVISION_WRAPPERS[tag],), fields, out, unsupported
            )
        elif tag == _W_SDT:
            content = child.find(_W_SDT_CONTENT)
            if content is not None:
                _walk_inline(content, route + (CONTENT_CONTROL_ROUTE,), fields, out, unsupported)
        elif tag in _INLINE_WRAPPERS:
            _walk_inline(child, route + (_INLINE_WRAPPERS[tag],), fields, out, unsupported)
        elif tag in _MATH_TAGS:
            if unsupported is not None:
                unsupported.equations.add(child)


def _paragraph_segments(p_el, unsupported=None) -> list[_Segment]:
    """A paragraph's visible text, as segments with their provenance.

    The Accept-All view, with every inline wrapper read and every field
    instruction left out. Shared with the applier, which must match edits
    against exactly the text the review saw and refuse the parts it cannot
    write (see :class:`_Segment`).
    """
    out: list[_Segment] = []
    _walk_inline(p_el, (), _FieldState(), out, unsupported)
    return out


def _accept_all_paragraph_text(p_el, unsupported=None) -> str:
    """Return a paragraph element's visible text as if all tracked changes were accepted.

    Includes the text of inline content controls, smart tags, custom XML
    elements, simple fields' stored results, and hyperlinks (revisions inside
    them included); excludes deletions, move sources, and field instructions.
    For a paragraph with none of that markup this equals python-docx
    ``Paragraph.text``. See :func:`_paragraph_segments`.
    """
    return "".join(segment.text for segment in _paragraph_segments(p_el, unsupported))


def _block_wrapper_content(wrapper) -> list:
    """The children a block wrapper shows, in order: a content control's
    ``<w:sdtContent>`` children, or a custom XML element's children after its
    properties. Comments and processing instructions are skipped.

    Element ids index this list (``cc<n>p<i>`` names its entry ``i``), so the
    extractor and any resolver must both use this function.
    """
    if wrapper.tag == _W_SDT:
        content = wrapper.find(_W_SDT_CONTENT)
        children = list(content) if content is not None else []
    else:
        children = [child for child in wrapper if child.tag != _W_CUSTOM_XML_PR]
    return [child for child in children if isinstance(child.tag, str)]


_TABLE_OF_CONTENTS_GALLERY = "Table of Contents"


def _is_table_of_contents(wrapper) -> bool:
    """Whether a block content control is a Word table of contents.

    Word inserts a table of contents as a content control of the "Table of
    Contents" building-block gallery. Its entries are a field result generated
    from the document's own headings, which are read where they stand; reading
    the copy as well would repeat every heading (and make each look like a
    duplicate heading to the deterministic checks). It is skipped on purpose,
    not lost.
    """
    if wrapper.tag != _W_SDT:
        return False
    properties = wrapper.find(_W_SDT_PR)
    if properties is None:
        return False
    gallery = properties.find(f"{_W_DOC_PART_OBJ}/{_W_DOC_PART_GALLERY}")
    return gallery is not None and gallery.get(_W_VAL) == _TABLE_OF_CONTENTS_GALLERY


def _cell_paragraphs(tc, unsupported=None) -> list:
    """The paragraphs whose text makes up a table cell's text, in document order.

    The cell's own paragraphs and the paragraphs of any block content control
    (at any depth) inside the cell — never the paragraphs of a table nested in
    the cell: those rows are extracted separately (or, inside a control,
    reported as unread), so keeping this list paragraphs-only is what
    guarantees nested text is never counted twice. The applier resolves a
    table row to exactly these paragraphs. An equation standing at block
    level in the cell is recorded in ``unsupported`` when the caller keeps one.
    """
    found: list = []

    def walk(children) -> None:
        for child in children:
            tag = child.tag
            if tag == _W_P:
                found.append(child)
            elif tag in _BLOCK_WRAPPERS and not _is_table_of_contents(child):
                walk(_block_wrapper_content(child))
            elif tag in _MATH_TAGS and unsupported is not None:
                unsupported.equations.add(child)

    walk(tc)
    return found


def _cell_text(tc, unsupported=None) -> str:
    """Accept-All text of a table cell: its paragraphs joined by newlines.

    Matches python-docx ``_Cell.text`` for a cell with no wrappers, but
    resolves each paragraph through the revision- and wrapper-aware walk and
    includes paragraphs inside the cell's block content controls.
    """
    return "\n".join(
        _accept_all_paragraph_text(p, unsupported) for p in _cell_paragraphs(tc, unsupported)
    )


def _unread_cell_tables(tc, *, wrapped_cell: bool) -> list:
    """Tables in a cell that the table walk does not read.

    A table directly in an ordinary cell is a nested table and is read. One
    inside a block content control in the cell is not, and neither is any
    table in a cell that is itself wrapped in a control (``wrapped_cell``).
    """
    found: list = []

    def walk(children, unread: bool) -> None:
        for child in children:
            tag = child.tag
            if tag == _W_TBL:
                if unread:
                    found.append(child)
            elif tag in _BLOCK_WRAPPERS and not _is_table_of_contents(child):
                walk(_block_wrapper_content(child), True)

    walk(tc, wrapped_cell)
    return found


def _element_has_tracked_changes(el) -> bool:
    """True when ``el`` contains any pending tracked-change markup."""
    return any(el.find(".//" + tag) is not None for tag in _REVISION_MARKER_TAGS)


def _story_paragraphs(story, unsupported=None) -> list[tuple]:
    """``(ordinal, wrapped_path, paragraph)`` for each paragraph a story shows.

    A story here is a header, footer, text box, or note. Its own paragraphs
    keep their legacy position — ``ordinal`` is the index among the story's
    ``<w:p>`` children, counted whether or not they hold text, and
    ``wrapped_path`` is ``None``. A paragraph inside a block content control
    gets ``ordinal=None`` and a ``wrapped_path`` of ``cc<K>p<i>``: ``K`` is the
    control's physical child index in the story, ``i`` the paragraph's index in
    the control's content, and each nested control adds a ``cc<i>`` step.
    Tables are not read in these stories, inside a control or not (a known
    gap, see CLAUDE.md "DOCX supplemental content extraction"). An equation
    standing at block level is recorded in ``unsupported`` when the caller
    keeps one.
    """
    found: list[tuple] = []

    def record_unread(child) -> None:
        if child.tag in _MATH_TAGS and unsupported is not None:
            unsupported.equations.add(child)

    def walk_wrapper(wrapper, prefix: str) -> None:
        if _is_table_of_contents(wrapper):
            return
        for index, child in enumerate(_block_wrapper_content(wrapper)):
            tag = child.tag
            if tag == _W_P:
                found.append((None, f"{prefix}p{index}", child))
            elif tag in _BLOCK_WRAPPERS:
                walk_wrapper(child, f"{prefix}cc{index}")
            else:
                record_unread(child)

    ordinal = 0
    for position, child in enumerate(story):
        tag = child.tag
        if tag == _W_P:
            found.append((ordinal, None, child))
            ordinal += 1
        elif tag in _BLOCK_WRAPPERS:
            walk_wrapper(child, f"cc{position}")
        else:
            record_unread(child)
    return found


def _document_has_tracked_changes(doc) -> bool:
    """True when the document carries pending tracked-change markup on any
    surface the extractor reads.

    The advisory must mirror extraction coverage so a reviewer is always told
    when *any* extracted text was resolved to the Accept-All view — not just
    body text. Revision markup can live in three places the extractor reads:
    the body (including tables, content controls, and text boxes, all nested
    under ``<w:body>``), the section headers/footers (their paragraphs, and
    the paragraphs of their block content controls), and the footnote/endnote
    package parts. Header/footer and note parts hang off the document by
    relationship, so the body scan alone misses a redline confined to them
    (e.g. a revision note in a page header). Parsing of note parts is
    defensive — an unreadable part never sinks detection, matching
    ``_collect_note_mappings``.
    """
    if _element_has_tracked_changes(doc.element.body):
        return True
    for section in doc.sections:
        for container in (section.header, section.footer):
            if any(
                _element_has_tracked_changes(p_el)
                for _, _, p_el in _story_paragraphs(container._element)
            ):
                return True
    for content_type in (_FOOTNOTES_CONTENT_TYPE, _ENDNOTES_CONTENT_TYPE):
        note_part = _find_part_by_content_type(doc.part, content_type)
        if note_part is None:
            continue
        try:
            root = parse_xml(note_part.blob)
        except Exception:
            continue
        if _element_has_tracked_changes(root):
            return True
    return False


# ---------------------------------------------------------------------------
# Table walk: merged cells, nested tables, and wrapped rows and cells
# ---------------------------------------------------------------------------

# Deepest table nesting the walk descends into (a top-level body table is
# depth 1). Specs authored inside a one-cell layout table routinely nest
# their schedule tables one level down; nothing real nests four levels deep,
# so the bound only keeps the recursion finite on a pathological document.
# Text below the bound is not extracted, and the spec is flagged with an
# extraction warning rather than losing that text silently.
_NESTED_TABLE_MAX_DEPTH = 4
_NESTED_TABLE_DEPTH_WARNING = (
    f"Spec contains tables nested more than {_NESTED_TABLE_MAX_DEPTH} levels deep; "
    "text below that depth was not extracted for review. Verify visually."
)


def _unique_row_cells(row, seen_tcs: set) -> list:
    """Return the distinct cells of ``row`` in grid order, each exactly once.

    python-docx's ``_Row.cells`` approximates a uniform grid: a horizontally
    merged ``<w:tc>`` (``gridSpan``) is returned once per grid column it
    spans, and the continuation rows of a vertically merged cell
    (``vMerge``) resolve to the origin ``<w:tc>`` in the row above. Walking
    it naively emits a three-column merged heading three times on its row
    and a three-row merged label once per spanned row. The XML stores the
    text exactly once — in the origin ``<w:tc>`` — so the walk dedupes on
    that element's identity across the whole table (``seen_tcs`` is shared
    by every row of one table): each cell contributes its text once, in the
    row and column where it originates. Holding the elements in
    ``seen_tcs`` keeps their lxml proxies alive, which is what makes
    identity stable across successive ``row.cells`` calls.

    These are the row's own ``<w:tc>`` children only; the ``c<n>`` step of a
    nested-table id indexes this list. Cells wrapped in a content control are
    added to the row's text by :func:`_row_text_cells`.
    """
    cells = []
    for cell in row.cells:
        tc = cell._tc
        if tc in seen_tcs:
            continue
        seen_tcs.add(tc)
        cells.append(cell)
    return cells


def _is_vertical_merge_continuation(tc) -> bool:
    """A ``<w:tc>`` continuing a vertical merge holds no text of its own; Word
    shows the merged region with the origin cell's content."""
    return getattr(tc, "vMerge", None) == "continue"


def _row_text_cells(tr, direct_cells: list) -> list:
    """The ``<w:tc>`` elements whose text makes up a row's text, in document order.

    ``direct_cells`` is the row's :func:`_unique_row_cells` list. A row with
    no cell-level content control reads exactly those cells, in that order.
    A row whose ``<w:tr>`` wraps cells in a content control (Word does this
    when a whole cell is selected for a control) reads the wrapped cells too,
    in document order among the others; a wrapped cell continuing a vertical
    merge is skipped like any other. The applier resolves a table row through
    this same list.
    """
    tcs = [cell._tc for cell in direct_cells]
    if not any(child.tag in _BLOCK_WRAPPERS for child in tr):
        return tcs
    direct = set(tcs)
    placed: set = set()
    ordered: list = []

    def walk(children, wrapped: bool) -> None:
        for child in children:
            tag = child.tag
            if tag == _W_TC:
                if not wrapped:
                    if child in direct and child not in placed:
                        placed.add(child)
                        ordered.append(child)
                elif not _is_vertical_merge_continuation(child):
                    ordered.append(child)
            elif tag in _BLOCK_WRAPPERS:
                walk(_block_wrapper_content(child), True)

    walk(tr, False)
    # A direct cell whose element is not in this row (python-docx resolves a
    # vertical-merge continuation to its origin; the dedup normally drops it)
    # keeps its text rather than losing it.
    ordered.extend(tc for tc in tcs if tc not in placed)
    return ordered


def _wrapped_row_cells(tr) -> list:
    """The cells of a row that sits inside a content control, in document order.

    python-docx's merge handling assumes a row is a direct child of its table,
    so these rows are read from the XML: each ``<w:tc>`` (including cells
    wrapped in a cell-level control), skipping vertical-merge continuations.
    """
    ordered: list = []

    def walk(children) -> None:
        for child in children:
            tag = child.tag
            if tag == _W_TC:
                if not _is_vertical_merge_continuation(child):
                    ordered.append(child)
            elif tag in _BLOCK_WRAPPERS:
                walk(_block_wrapper_content(child))

    walk(tr)
    return ordered


def _append_row_mapping(
    tcs: list,
    *,
    paragraphs: list[str],
    paragraph_map: list[ParagraphMapping],
    body_index: int,
    table_index: int | None,
    row_index: int,
    element_id: str,
    container_type: str | None,
    section_id: str,
    unsupported,
) -> None:
    """Append one row: its cells' non-empty text joined with ``" | "``."""
    row_text = [text for tc in tcs if (text := _cell_text(tc, unsupported).strip())]
    if not row_text:
        return
    joined_text = " | ".join(row_text)
    paragraphs.append(joined_text)
    paragraph_map.append(
        ParagraphMapping(
            body_index=body_index,
            element_type="table_cell",
            text=joined_text,
            table_index=table_index,
            row_index=row_index,
            cell_index=None,
            container_type=container_type,
            element_id=element_id,
            section_id=section_id,
        )
    )


def _collect_table_mappings(
    table,
    *,
    paragraphs: list[str],
    paragraph_map: list[ParagraphMapping],
    warnings: list[str],
    body_index: int,
    table_index: int | None,
    id_prefix: str,
    section_id: str,
    depth: int,
    wrapped: bool = False,
    unsupported: _Unsupported | None = None,
) -> None:
    """Append one mapping per non-empty row of ``table``, then recurse into
    the tables nested in its cells.

    Each row renders as its cells' text joined with ``" | "`` under the id
    ``{id_prefix}r<row>`` (``t<table>r<row>`` at the top level), where
    ``<row>`` counts the table's own ``<w:tr>`` children. The rows of a table
    nested in a cell follow the row that contains them, in cell order, under
    ``{row_id}c<cell>t<nested>r<row>`` — ``t0r1c0t0r0`` is row 0 of the first
    table nested in cell 0 of row 1 of body table 0. The path form cannot
    collide with any other id. Nested rows keep ``element_type="table_cell"``
    (they render as ``<row>`` in the prompt) and carry
    ``container_type="nested_table"``; ``table_index`` stays the body table's
    index and ``row_index`` is the row's index within its own table. A cell's
    own paragraphs never include its nested tables' text
    (:func:`_cell_paragraphs`), so nothing is emitted twice, and a cell that
    holds only a nested table still surfaces that table even though its own
    row emits no text. ``paragraphs`` and ``paragraph_map`` are appended in
    lockstep so the reconstruction invariant holds.

    Content controls inside a table (plan WP-02): a control wrapping whole
    rows (a repeating section) is read as rows of its own under
    ``{id_prefix}cc<k>r<i>`` — ``k`` the control's physical child index in the
    ``<w:tbl>``, ``i`` the row's index in the control's content — so the
    table's ordinary rows keep their numbers. A control wrapping a cell, or
    wrapping paragraphs inside a cell, adds its text to the row that holds it,
    in document order. A table inside any of those controls is not read and is
    reported as an extraction warning. ``wrapped`` marks a table that itself
    sits in a block content control: all of its rows, nested ones included,
    carry ``container_type="content_control"``.
    """
    seen_tcs: set = set()
    rows = list(table.rows)
    row_container = (
        CONTENT_CONTROL_CONTAINER if wrapped else ("nested_table" if depth > 1 else None)
    )
    direct_row_index = 0
    for position, child in enumerate(table._tbl):
        tag = child.tag
        if tag in _BLOCK_WRAPPERS:
            _collect_wrapped_rows(
                child,
                prefix=f"{id_prefix}cc{position}",
                paragraphs=paragraphs,
                paragraph_map=paragraph_map,
                body_index=body_index,
                table_index=table_index,
                section_id=section_id,
                unsupported=unsupported,
            )
            continue
        if tag != _W_TR:
            continue
        row_index = direct_row_index
        direct_row_index += 1
        row = rows[row_index]
        cells = _unique_row_cells(row, seen_tcs)
        text_cells = _row_text_cells(child, cells)
        row_id = f"{id_prefix}r{row_index}"
        _append_row_mapping(
            text_cells,
            paragraphs=paragraphs,
            paragraph_map=paragraph_map,
            body_index=body_index,
            table_index=table_index,
            row_index=row_index,
            element_id=row_id,
            container_type=row_container,
            section_id=section_id,
            unsupported=unsupported,
        )
        if unsupported is not None:
            direct = {cell._tc for cell in cells}
            for tc in text_cells:
                unsupported.table_control_tables.update(
                    _unread_cell_tables(tc, wrapped_cell=tc not in direct)
                )
        for cell_index, cell in enumerate(cells):
            nested_tables = cell.tables
            if not nested_tables:
                continue
            if depth >= _NESTED_TABLE_MAX_DEPTH:
                if _NESTED_TABLE_DEPTH_WARNING not in warnings:
                    warnings.append(_NESTED_TABLE_DEPTH_WARNING)
                continue
            for nested_index, nested in enumerate(nested_tables):
                _collect_table_mappings(
                    nested,
                    paragraphs=paragraphs,
                    paragraph_map=paragraph_map,
                    warnings=warnings,
                    body_index=body_index,
                    table_index=table_index,
                    id_prefix=f"{row_id}c{cell_index}t{nested_index}",
                    section_id=section_id,
                    depth=depth + 1,
                    wrapped=wrapped,
                    unsupported=unsupported,
                )


def _collect_wrapped_rows(
    wrapper,
    *,
    prefix: str,
    paragraphs: list[str],
    paragraph_map: list[ParagraphMapping],
    body_index: int,
    table_index: int | None,
    section_id: str,
    unsupported: _Unsupported | None,
) -> None:
    """Append the rows a row-level content control wraps (``{prefix}r<i>``).

    ``i`` is the row's index in the control's content; a nested control adds a
    ``cc<i>`` step. Every cell of these rows is read (:func:`_wrapped_row_cells`);
    a table inside one of them is not, and is counted as unread.
    """
    for index, child in enumerate(_block_wrapper_content(wrapper)):
        tag = child.tag
        if tag == _W_TR:
            tcs = _wrapped_row_cells(child)
            _append_row_mapping(
                tcs,
                paragraphs=paragraphs,
                paragraph_map=paragraph_map,
                body_index=body_index,
                table_index=table_index,
                row_index=index,
                element_id=f"{prefix}r{index}",
                container_type=CONTENT_CONTROL_CONTAINER,
                section_id=section_id,
                unsupported=unsupported,
            )
            if unsupported is not None:
                for tc in tcs:
                    unsupported.table_control_tables.update(
                        _unread_cell_tables(tc, wrapped_cell=True)
                    )
        elif tag in _BLOCK_WRAPPERS:
            _collect_wrapped_rows(
                child,
                prefix=f"{prefix}cc{index}",
                paragraphs=paragraphs,
                paragraph_map=paragraph_map,
                body_index=body_index,
                table_index=table_index,
                section_id=section_id,
                unsupported=unsupported,
            )


def extract_text_from_docx(filepath: Path) -> ExtractedSpec:
    if not filepath.exists():
        raise FileNotFoundError(f"File not found: {filepath}")
    if filepath.suffix.lower() != ".docx":
        raise ValueError(f"Not a .docx file: {filepath}")
    try:
        doc = Document(filepath)
    except PackageNotFoundError:
        raise ValueError(f"Invalid or corrupted .docx file: {filepath}")
    except Exception as e:
        raise ValueError(f"Could not read .docx file: {filepath} — {e}")

    paragraphs: list[str] = []
    paragraph_map: list[ParagraphMapping] = []
    table_counter = 0
    # Warnings raised by the table walk (nesting deeper than the bound);
    # merged into ``extraction_warnings`` after the content-loss scan.
    table_warnings: list[str] = []
    # Text-bearing structures the walk meets but does not read (plan WP-02):
    # reported as extraction warnings after the table warnings.
    unsupported = _Unsupported()
    # Track the most recently seen heading paragraph so each
    # element below it can carry a ``section_id``. Reset to empty when the
    # extractor crosses a top-level "PART ..." boundary so subsequent
    # subheadings nest under the right ancestor.
    current_section: str = ""

    def add_paragraph(p_el, *, body_index: int, element_id: str, container_type=None) -> None:
        nonlocal current_section
        text = _accept_all_paragraph_text(p_el, unsupported).strip()
        if not text:
            return
        paragraphs.append(text)
        if _is_heading_paragraph(text):
            current_section = text
        paragraph_map.append(
            ParagraphMapping(
                body_index=body_index,
                element_type="paragraph",
                text=text,
                table_index=None,
                row_index=None,
                cell_index=None,
                container_type=container_type,
                element_id=element_id,
                section_id=current_section,
            )
        )

    def add_table(tbl_el, *, body_index: int, table_index, id_prefix: str, wrapped=False) -> None:
        # Merged cells are emitted once and nested tables are walked
        # (depth-bounded) — see ``_collect_table_mappings``.
        _collect_table_mappings(
            DocxTable(tbl_el, doc),
            paragraphs=paragraphs,
            paragraph_map=paragraph_map,
            warnings=table_warnings,
            body_index=body_index,
            table_index=table_index,
            id_prefix=id_prefix,
            section_id=current_section,
            depth=1,
            wrapped=wrapped,
            unsupported=unsupported,
        )

    def add_block_wrapper(wrapper, *, body_index: int, prefix: str) -> None:
        # A block content control (or custom XML block): its paragraphs and
        # tables are read in place, in document order, under ids of their own
        # (``{prefix}p<i>`` / ``{prefix}t<i>…``, ``i`` the index in the
        # control's content), so the legacy ``pN`` / ``tN`` ids keep their
        # meaning. A table of contents is skipped on purpose (its entries
        # repeat the headings; see ``_is_table_of_contents``).
        if _is_table_of_contents(wrapper):
            return
        for index, child in enumerate(_block_wrapper_content(wrapper)):
            tag = child.tag
            if tag == _W_P:
                add_paragraph(
                    child,
                    body_index=body_index,
                    element_id=f"{prefix}p{index}",
                    container_type=CONTENT_CONTROL_CONTAINER,
                )
            elif tag == _W_TBL:
                add_table(
                    child,
                    body_index=body_index,
                    table_index=None,
                    id_prefix=f"{prefix}t{index}",
                    wrapped=True,
                )
            elif tag in _BLOCK_WRAPPERS:
                add_block_wrapper(child, body_index=body_index, prefix=f"{prefix}cc{index}")
            elif tag in _MATH_TAGS:
                # A display equation can stand at block level (the schema
                # allows it beside paragraphs); it is not read, only counted.
                unsupported.equations.add(child)

    for body_index, child in enumerate(doc.element.body):
        tag = child.tag
        if tag == _W_P:
            add_paragraph(child, body_index=body_index, element_id=f"p{body_index}")
        elif tag == _W_TBL:
            # ``t<n>`` counts the body's own tables only (python-docx's
            # ``Document.tables``): a table inside a content control never
            # renumbers them.
            add_table(
                child,
                body_index=body_index,
                table_index=table_counter,
                id_prefix=f"t{table_counter}",
            )
            table_counter += 1
        elif tag in _BLOCK_WRAPPERS:
            add_block_wrapper(child, body_index=body_index, prefix=f"cc{body_index}")
        elif tag in _MATH_TAGS:
            unsupported.equations.add(child)
    unsupported.alt_chunks.update(doc.element.body.iter(_W_ALT_CHUNK))

    header_footer_entries: list[ParagraphMapping] = []
    for section_index, section in enumerate(doc.sections):
        for container_name, container in (("header", section.header), ("footer", section.footer)):
            container_tag = "h" if container_name == "header" else "f"
            story = container._element
            unsupported.alt_chunks.update(story.iter(_W_ALT_CHUNK))
            # The story's own paragraphs keep ``s<n>h<i>`` (``i`` their index
            # among its paragraphs); a block content control's paragraphs get
            # ``s<n>hcc<k>p<i>`` (see ``_story_paragraphs``).
            for ordinal, wrapped_path, para_el in _story_paragraphs(story, unsupported):
                text = _accept_all_paragraph_text(para_el, unsupported).strip()
                if not text:
                    continue
                if wrapped_path is None:
                    element_id = f"s{section_index}{container_tag}{ordinal}"
                    container_type = container_name
                else:
                    element_id = f"s{section_index}{container_tag}{wrapped_path}"
                    container_type = CONTENT_CONTROL_CONTAINER
                header_footer_entries.append(
                    ParagraphMapping(
                        body_index=-1,
                        element_type=container_name,
                        text=f"[{container_name.title()}] {text}",
                        table_index=None,
                        row_index=None,
                        cell_index=None,
                        section_index=section_index,
                        container_type=container_type,
                        element_id=element_id,
                        section_id="",
                    )
                )

    # Supplemental content that python-docx's body walk does not surface:
    # text boxes (text nested in <w:txbxContent> inside drawings / VML) and
    # footnotes / endnotes (separate package parts). A requirement authored
    # in any of these would otherwise be invisible to the reviewer — a
    # silent "miss a real problem" gap (TRUST_AUDIT P0-6). Each kind is
    # rendered as its own labeled block after the body (mirroring the
    # header/footer block); the blocks no-op when their source is absent, so
    # a spec with none of them produces byte-identical output to before.
    _append_supplemental_block(
        paragraphs,
        paragraph_map,
        delimiter="===== TEXT BOX CONTENT =====",
        delimiter_id="meta:tb",
        container_type="textbox",
        entries=_collect_textbox_mappings(doc.element.body, unsupported),
    )
    _append_supplemental_block(
        paragraphs,
        paragraph_map,
        delimiter="===== FOOTNOTE CONTENT =====",
        delimiter_id="meta:fn",
        container_type="footnote",
        entries=_collect_note_mappings(
            doc.part,
            content_type=_FOOTNOTES_CONTENT_TYPE,
            note_tag="w:footnote",
            label="Footnote",
            id_prefix="fn",
            unsupported=unsupported,
        ),
    )
    _append_supplemental_block(
        paragraphs,
        paragraph_map,
        delimiter="===== ENDNOTE CONTENT =====",
        delimiter_id="meta:en",
        container_type="endnote",
        entries=_collect_note_mappings(
            doc.part,
            content_type=_ENDNOTES_CONTENT_TYPE,
            note_tag="w:endnote",
            label="Endnote",
            id_prefix="en",
            unsupported=unsupported,
        ),
    )
    _append_supplemental_block(
        paragraphs,
        paragraph_map,
        delimiter="===== HEADER/FOOTER CONTENT =====",
        delimiter_id="meta:hf",
        container_type="header_footer",
        entries=header_footer_entries,
    )

    content = "\n\n".join(paragraphs)
    reconstructed = "\n\n".join(m.text for m in paragraph_map)
    if reconstructed != content:
        # Controlled error preserves context (audit Issue 10). The raw assert
        # version was stripped under -O and produced an opaque AssertionError.
        raise ValueError(
            f"Paragraph map for '{filepath.name}' does not reconstruct extracted content "
            f"(map_chars={len(reconstructed)}, content_chars={len(content)})."
        )

    # Scan the body for embedded drawings /
    # pictures / objects. When the proportion of non-text elements
    # exceeds the threshold, the spec is likely drawing-heavy and text-
    # only extraction may have missed reviewable content. The warning
    # rides on ``extraction_warnings`` so the run-diagnostics banner can
    # count affected specs and surface the count to the reviewer.
    extraction_warnings: list[str] = []
    content_loss_warning = _detect_content_loss_warning(doc.element.body)
    if content_loss_warning is not None:
        extraction_warnings.append(content_loss_warning)
    extraction_warnings.extend(table_warnings)
    # Structures the walk met but could not read (equations, embedded
    # documents, legacy drop-down form fields, tables inside table content
    # controls). Each warning names what was found and how many — never a
    # figure for how much text that is, which the extractor cannot know.
    extraction_warnings.extend(unsupported.warnings())

    # The extracted ``content`` above is the Accept-All-Changes view. Flag
    # whether any pending revision markup was present on any extracted surface
    # (body, headers/footers, footnote/endnote parts) so the report can advise
    # the reviewer the spec was read as accept-all (see ``tracked_changes_detected``).
    tracked_changes_detected = _document_has_tracked_changes(doc)

    return ExtractedSpec(
        filename=filepath.name,
        content=content,
        word_count=len(content.split()),
        source_path=str(filepath),
        source_format="docx",
        paragraph_map=paragraph_map,
        document_id=_derive_document_id(filepath.name),
        extraction_warnings=extraction_warnings,
        tracked_changes_detected=tracked_changes_detected,
    )


def extract_text(filepath: Path) -> ExtractedSpec:
    filepath = Path(filepath)
    ext = filepath.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file format: '{ext}'. Supported formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    return extract_text_from_docx(filepath)


def extract_multiple_specs(
    filepaths: list[Path],
    *,
    max_workers: int | None = None,
) -> list[ExtractedSpec]:
    """Extract a list of specs in parallel.

    Phase 5.2 (audit Section 9.2): bounded thread pool for I/O-bound DOCX
    parsing. Result order is preserved to match ``filepaths`` so downstream
    deterministic ordering (filenames, dedup keys, request maps) does not
    change. ``max_workers=1`` (or a single file) runs sequentially.
    """
    if not filepaths:
        return []
    paths = [Path(fp) for fp in filepaths]
    if len(paths) == 1:
        return [extract_text(paths[0])]
    workers = max_workers if max_workers is not None else min(8, len(paths))
    workers = max(1, workers)
    if workers == 1:
        return [extract_text(p) for p in paths]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(extract_text, paths))


def _extract_pdf_text(filepath: Path) -> str:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise ValueError(
            "PDF support requires the 'pypdf' package. Install with: pip install pypdf"
        ) from exc
    try:
        reader = PdfReader(str(filepath))
    except PdfReadError as exc:
        raise ValueError(f"Invalid or corrupted PDF: {filepath} — {exc}")
    except Exception as exc:
        raise ValueError(f"Could not read PDF: {filepath} — {exc}")
    if getattr(reader, "is_encrypted", False):
        raise ValueError(f"PDF is encrypted and cannot be read: {filepath}")
    pages: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = text.strip()
        if text:
            pages.append(text)
    return "\n\n".join(pages)


def _extract_plaintext(filepath: Path) -> str:
    """Read a UTF-8 Markdown / plain-text context attachment verbatim.

    External reference notes (for example a drawing-set digest produced by a
    separate analysis tool) are often Markdown, and reviewers may keep project
    notes as ``.txt``; both are reference material spliced into
    ``project_context`` as-is. Undecodable bytes
    are replaced rather than raising, so one stray byte never sinks the whole
    attachment (the result is reviewed by a human-readable model, not parsed).
    """
    return filepath.read_text(encoding="utf-8", errors="replace")


def extract_context_text(filepath: Path) -> str:
    """Extract plain text from a Project Context attachment.

    Accepts ``.docx`` / ``.pdf`` (text extracted) and ``.md`` / ``.txt`` (read
    verbatim). Returns a plain string suitable for splicing into the
    project_context prompt block. Unlike ``extract_text``, this does not build a
    paragraph map — the result is reference material, not an editable spec.
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"File not found: {filepath}")
    ext = filepath.suffix.lower()
    if ext == ".docx":
        return extract_text_from_docx(filepath).content
    if ext == ".pdf":
        return _extract_pdf_text(filepath)
    if ext in {".md", ".txt"}:
        return _extract_plaintext(filepath)
    raise ValueError(
        f"Unsupported context attachment format: '{ext}'. "
        f"Supported: {', '.join(sorted(CONTEXT_ATTACHMENT_EXTENSIONS))}"
    )
