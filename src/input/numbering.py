"""Word automatic numbering, resolved to the labels Word displays (plan WP-03).

A paragraph Word numbers automatically ("1.01", "A.", "PART 2") stores no
number in its text: the number comes from a list definition in
``word/numbering.xml`` and from counting, in document order, every paragraph
of the same list. Without this module the review never saw an article's
number, and the structure checks and section attribution could not see an
automatically numbered heading at all.

The labels are **display text, not document text**. No run holds them, so an
edit can never change one; the extractor keeps each label's span beside the
text it prefixes (``ParagraphMapping.label_spans``) and the applier refuses an
edit that would have to change one.

What is resolved (Word's own model, checked on these points against
LibreOffice Writer 24.2, which imports DOCX numbering to match Word):

* **Where a paragraph's numbering comes from.** ``w:pPr/w:numPr`` on the
  paragraph, else its paragraph style (``w:pStyle``, else the default
  paragraph style), following ``w:basedOn``. The list (``w:numId``) and the
  level (``w:ilvl``) are inherited independently, so a style that names only
  a level takes the list from the style it is based on. ``w:numId`` 0 turns
  numbering off. A missing level is level 0.
* **Which definition.** ``w:num`` names a ``w:abstractNum``; an abstract
  definition that is only a ``w:numStyleLink`` to a numbering style is
  followed to the definition that style names. A ``w:lvlOverride`` may
  replace a level (``w:lvl``) or its start (``w:startOverride``).
* **Counters.** One set per *list*, a list being the resolved abstract
  definition: every ``w:num`` of one abstract definition continues the same
  counters (Word's "Continue numbering"). A level's first use shows its
  ``w:start`` (default 0); each later use adds one. Using a level restarts
  every deeper level unless its ``w:lvlRestart`` says otherwise (0: never;
  ``n``: only when level ``n`` or an earlier one is used). Counters are kept
  per document — a resolver is built for one document and never shared.
* **Labels.** ``w:lvlText`` with each ``%k`` replaced by level ``k``'s
  counter in that level's format (Arabic numerals throughout on a level with
  ``w:isLgl``), followed by the level's suffix (a tab or a space is shown as
  one space, ``nothing`` as nothing). Formats: ``decimal``, ``decimalZero``,
  ``upperLetter``, ``lowerLetter``, ``upperRoman``, ``lowerRoman``, and
  ``none``. A bullet carries no number and is not a label.
* **Which paragraphs count.** Every paragraph of the main document text in
  document order — body, tables at every depth, and content controls,
  including paragraphs the extractor does not read. A paragraph whose mark
  is a tracked deletion does not exist once changes are accepted, so it
  neither counts nor gets a label (the extractor reads the accept-all view).

What is **not** guessed. Where word processors may disagree, or the
definition does not say, a paragraph gets no label and a reason
(:data:`REASON_UNDEFINED`, :data:`REASON_UNSUPPORTED_FORMAT`,
:data:`REASON_AMBIGUOUS`) that the extractor turns into a warning. Ambiguous,
from the paragraph where the readings part and for the rest of that list:

* a list that is restarted by an override (``w:lvlOverride``) other than at
  its first level with every deeper level restarting too, or that goes back
  to an earlier ``w:num``, or continues in a plain ``w:num``, after such a
  restart (LibreOffice continues one shared counter there, a reading in
  which each restarted list is its own would not);
* a level first shown inside a deeper level's label (``3.1`` before any
  ``3.``) and then used directly: whether that first showing counted as a
  use is exactly what differs;
* a style linked to a list level by ``w:lvl/w:pStyle`` while the style names
  no level;
* a list also used in a header, footer, text box, or note, whose paragraphs
  Word may count among the main text's in an order the file does not record.

Numbering outside the main text (headers, footers, text boxes, notes) is not
resolved at all; :meth:`DocumentNumbering.numbered_outside_main_text` lets the
extractor warn about it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from docx.opc.constants import CONTENT_TYPE as CT
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import parse_xml
from docx.oxml.ns import qn

#: Why a numbered paragraph of the main text has no label.
REASON_UNDEFINED = "undefined"
REASON_UNSUPPORTED_FORMAT = "unsupported_format"
REASON_AMBIGUOUS = "ambiguous"

#: Number formats rendered as labels. ``none`` renders as nothing (the level
#: text's literal characters still show).
SUPPORTED_FORMATS = frozenset(
    {"decimal", "decimalZero", "upperLetter", "lowerLetter", "upperRoman", "lowerRoman", "none"}
)
_BULLET = "bullet"

_W_P = qn("w:p")
_W_PPR = qn("w:pPr")
_W_RPR = qn("w:rPr")
_W_NUMPR = qn("w:numPr")
_W_NUMID = qn("w:numId")
_W_NUM_ID_ATTR = _W_NUMID  # w:num/@w:numId
_W_ILVL = qn("w:ilvl")
_W_PSTYLE = qn("w:pStyle")
_W_VAL = qn("w:val")
_W_DEL = qn("w:del")
_W_MOVE_FROM = qn("w:moveFrom")
_W_TXBX_CONTENT = qn("w:txbxContent")
_W_ABSTRACT_NUM = qn("w:abstractNum")
_W_ABSTRACT_NUM_ID = qn("w:abstractNumId")
_W_NUM = qn("w:num")
_W_LVL = qn("w:lvl")
_W_LVL_OVERRIDE = qn("w:lvlOverride")
_W_START_OVERRIDE = qn("w:startOverride")
_W_START = qn("w:start")
_W_NUM_FMT = qn("w:numFmt")
_W_FORMAT = qn("w:format")
_W_LVL_TEXT = qn("w:lvlText")
_W_SUFF = qn("w:suff")
_W_LVL_RESTART = qn("w:lvlRestart")
_W_IS_LGL = qn("w:isLgl")
_W_LVL_PIC_BULLET_ID = qn("w:lvlPicBulletId")
_W_STYLE_LINK = qn("w:styleLink")
_W_NUM_STYLE_LINK = qn("w:numStyleLink")
_W_STYLE = qn("w:style")
_W_TYPE = qn("w:type")
_W_STYLE_ID = qn("w:styleId")
_W_DEFAULT = qn("w:default")
_W_BASED_ON = qn("w:basedOn")

_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_MC_ALTERNATE_CONTENT = f"{{{_MC_NS}}}AlternateContent"
_MC_BRANCHES = frozenset({f"{{{_MC_NS}}}Choice", f"{{{_MC_NS}}}Fallback"})

_NOTE_CONTENT_TYPES = (CT.WML_FOOTNOTES, CT.WML_ENDNOTES)
_HEADER_FOOTER_RELATIONSHIPS = (RT.HEADER, RT.FOOTER)

_PLACEHOLDER_RE = re.compile(r"%(\d)")
_MAX_LEVEL = 8
_ON_VALUES = frozenset({"1", "true", "on"})
_OFF_VALUES = frozenset({"0", "false", "off"})


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------


class _Malformed(Exception):
    """A value the definition needs is not a valid number."""


def _int_val(element) -> int | None:
    """The ``w:val`` of ``element`` as an int, ``None`` when there is no element."""
    if element is None:
        return None
    raw = element.get(_W_VAL)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise _Malformed(raw) from None


def _on(element) -> bool:
    if element is None:
        return False
    raw = (element.get(_W_VAL) or "true").strip().lower()
    return raw not in _OFF_VALUES


@dataclass(frozen=True)
class _Level:
    ilvl: int
    start: int
    fmt: str
    text: str | None
    suffix: str
    restart: int | None
    legal: bool
    style: str | None

    @property
    def is_bullet(self) -> bool:
        return self.fmt == _BULLET


def _parse_level(lvl) -> _Level:
    ilvl = int(lvl.get(_W_ILVL))
    num_fmt = lvl.find(_W_NUM_FMT)
    if lvl.find(_W_LVL_PIC_BULLET_ID) is not None:
        fmt = _BULLET
    elif num_fmt is None:
        fmt = "decimal"
    elif num_fmt.get(_W_FORMAT):
        fmt = "custom"  # a custom format string (w:format) is not rendered
    else:
        fmt = num_fmt.get(_W_VAL) or "decimal"
    text_el = lvl.find(_W_LVL_TEXT)
    suffix_el = lvl.find(_W_SUFF)
    style_el = lvl.find(_W_PSTYLE)
    start = _int_val(lvl.find(_W_START))
    return _Level(
        ilvl=ilvl,
        start=0 if start is None else start,
        fmt=fmt,
        text=None if text_el is None else (text_el.get(_W_VAL) or ""),
        suffix=(suffix_el.get(_W_VAL) if suffix_el is not None else None) or "tab",
        restart=_int_val(lvl.find(_W_LVL_RESTART)),
        legal=_on(lvl.find(_W_IS_LGL)),
        style=None if style_el is None else style_el.get(_W_VAL),
    )


def _parse_levels(parent) -> dict[int, _Level] | None:
    """Levels by ``w:ilvl``, or ``None`` when one is malformed."""
    levels: dict[int, _Level] = {}
    for lvl in parent.findall(_W_LVL):
        try:
            level = _parse_level(lvl)
        except (_Malformed, TypeError, ValueError):
            return None
        if not 0 <= level.ilvl <= _MAX_LEVEL:
            return None
        levels.setdefault(level.ilvl, level)
    return levels


@dataclass(frozen=True)
class _Abstract:
    levels: dict[int, _Level] | None
    num_style_link: str | None


@dataclass(frozen=True)
class _Override:
    start: int | None
    level: _Level | None


@dataclass(frozen=True)
class _Num:
    abstract_id: str
    overrides: dict[int, _Override] | None


@dataclass(frozen=True)
class _Style:
    based_on: str | None
    num_id: int | None
    ilvl: int | None
    malformed: bool


@dataclass(frozen=True)
class _ListLevels:
    """One ``w:num``'s view of its list: the list's identity and the level
    definitions and starts that apply to paragraphs using this ``w:num``."""

    list_id: str
    num_id: int
    base: dict[int, _Level]
    overrides: dict[int, _Override]

    def level(self, ilvl: int) -> _Level | None:
        override = self.overrides.get(ilvl)
        if override is not None and override.level is not None:
            return override.level
        return self.base.get(ilvl)

    def start(self, ilvl: int) -> int:
        override = self.overrides.get(ilvl)
        if override is not None and override.start is not None:
            return override.start
        level = self.level(ilvl)
        return level.start if level is not None else 0


def _part_element(part):
    element = getattr(part, "element", None)
    if element is not None:
        return element
    try:
        return parse_xml(part.blob)
    except Exception:
        return None


def _related_parts(document_part, *, content_types=(), reltypes=()):
    for rel in document_part.rels.values():
        if rel.is_external:
            continue
        target = rel.target_part
        if rel.reltype in reltypes or getattr(target, "content_type", None) in content_types:
            yield target


@dataclass(frozen=True)
class NumberingLabel:
    """The number Word displays in front of one paragraph.

    ``text`` is the label (``"1.01"``, ``"A."``, ``"PART 2"``); ``separator``
    is what stands between it and the paragraph's text (``" "`` or ``""``).
    ``prefix`` is the synthetic text the extractor puts in front of the
    paragraph — no run holds any of it.
    """

    text: str
    separator: str
    list_id: str
    level: int

    @property
    def prefix(self) -> str:
        return self.text + self.separator


@dataclass
class _ListState:
    values: dict[int, int] = field(default_factory=dict)
    #: Levels whose counter is not known: shown inside a deeper level's label
    #: before their own first use, or left by a restart rule the standard
    #: does not settle. Using one is where readings part.
    unknown: set[int] = field(default_factory=set)
    used_nums: set[int] = field(default_factory=set)
    last_num: int | None = None
    restarted: bool = False
    ambiguous: bool = False


def _roman(value: int) -> str | None:
    if not 1 <= value <= 3999:
        return None
    pairs = (
        (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
        (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
    )
    out: list[str] = []
    for amount, numeral in pairs:
        while value >= amount:
            out.append(numeral)
            value -= amount
    return "".join(out)


def format_number(value: int, fmt: str) -> str | None:
    """``value`` in Word number format ``fmt``, or ``None`` when it cannot be
    written in that format (an unsupported format, a letter or roman numeral
    for zero, a negative number)."""
    if value < 0:
        return None
    if fmt == "decimal":
        return str(value)
    if fmt == "decimalZero":
        return f"{value:02d}"
    if fmt in ("upperLetter", "lowerLetter"):
        if value < 1:
            return None
        letter = chr(ord("A") + (value - 1) % 26) * ((value - 1) // 26 + 1)
        return letter if fmt == "upperLetter" else letter.lower()
    if fmt in ("upperRoman", "lowerRoman"):
        numeral = _roman(value)
        if numeral is None:
            return None
        return numeral if fmt == "upperRoman" else numeral.lower()
    if fmt == "none":
        return ""
    return None


#: What a number looks like in each format, for recognizing a typed number of
#: the same shape. A letter label repeats one letter (``A``, ``AA``), so the
#: letter formats are built per placeholder (see ``_format_shape``).
_FORMAT_PATTERNS = {
    "decimal": r"\d+",
    "decimalZero": r"\d{2,}",
    "upperRoman": r"[IVXLCDM]+",
    "lowerRoman": r"[ivxlcdm]+",
    "none": r"",
}
_LETTER_CLASSES = {"upperLetter": "[A-Z]", "lowerLetter": "[a-z]"}


def _format_shape(fmt: str, index: int) -> str | None:
    """The regex for one number in format ``fmt`` (``index`` names the group
    a repeated letter needs)."""
    letters = _LETTER_CLASSES.get(fmt)
    if letters is not None:
        return f"(?P<n{index}>{letters})(?P=n{index})*"
    return _FORMAT_PATTERNS.get(fmt)


class _Unresolved(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DocumentNumbering:
    """Every automatic number in one document's main text.

    Build one per document (:func:`resolve_numbering`); its counters belong to
    that document alone. Paragraphs are looked up by element identity, so
    look them up in the same tree the resolver walked.
    """

    def __init__(self, document) -> None:
        self._abstracts: dict[str, _Abstract] = {}
        self._nums: dict[int, _Num | None] = {}
        self._styles: dict[str, _Style] = {}
        self._default_style: str | None = None
        self._labels: dict = {}
        # What a labeled paragraph's label was rendered from, for the two
        # lookups only the applier's additions need (computed on demand).
        self._contexts: dict = {}
        self._patterns: dict = {}
        self._reasons: dict = {}
        self._outside_lists: set[str] = set()
        self._load_definitions(document)
        body = document.element.body
        self._collect_outside_lists(document, body)
        self._resolve_main_text(body)

    # -- public -----------------------------------------------------------
    def label(self, p_el) -> NumberingLabel | None:
        """The label Word shows in front of a main-text paragraph, if any."""
        return self._labels.get(p_el)

    def unresolved_reason(self, p_el) -> str | None:
        """Why a numbered main-text paragraph has no label, or ``None``."""
        return self._reasons.get(p_el)

    def next_label(self, p_el) -> NumberingLabel | None:
        """The label a paragraph numbered like ``p_el`` would get if it were
        inserted immediately after it: the same level, one further on."""
        context = self._contexts.get(p_el)
        if context is None:
            return None
        view, level, ilvl, values = context
        following = dict(values)
        following[ilvl] += 1
        text = self._render(view, level, ilvl, following, strict=False)
        if not text:
            return None
        return NumberingLabel(text, self._labels[p_el].separator, view.list_id, ilvl)

    def label_pattern(self, p_el) -> re.Pattern[str] | None:
        """A pattern matching any label of ``p_el``'s level at the start of a
        text (the level's text with each number in its format)."""
        context = self._contexts.get(p_el)
        if context is None:
            return None
        view, level, ilvl, _ = context
        key = (view.num_id, ilvl)
        if key not in self._patterns:
            self._patterns[key] = self._pattern(view, level, ilvl)
        return self._patterns[key]

    def numbered_outside_main_text(self, p_el) -> bool:
        """Whether a header, footer, text box, or note paragraph is numbered.

        Numbering outside the main text is not resolved; the extractor warns
        about each such paragraph instead of showing no number silently.
        Bullets are not numbers.
        """
        try:
            located = self._locate(p_el)
        except _Unresolved:
            return True
        if located is None:
            return False
        view, ilvl, _ = located
        level = view.level(ilvl)
        return level is None or not level.is_bullet

    # -- definitions ------------------------------------------------------
    def _load_definitions(self, document) -> None:
        document_part = document.part
        styles = next(_related_parts(document_part, content_types=(CT.WML_STYLES,)), None)
        root = _part_element(styles) if styles is not None else None
        if root is not None:
            for style in root.findall(_W_STYLE):
                if style.get(_W_TYPE) not in ("paragraph", "numbering"):
                    continue
                style_id = style.get(_W_STYLE_ID)
                if not style_id:
                    continue
                based_on = style.find(_W_BASED_ON)
                num_pr = style.find(f"{_W_PPR}/{_W_NUMPR}")
                num_id = ilvl = None
                malformed = False
                if num_pr is not None:
                    try:
                        num_id = _int_val(num_pr.find(_W_NUMID))
                        ilvl = _int_val(num_pr.find(_W_ILVL))
                    except _Malformed:
                        malformed = True
                self._styles.setdefault(
                    style_id,
                    _Style(
                        based_on=None if based_on is None else based_on.get(_W_VAL),
                        num_id=num_id,
                        ilvl=ilvl,
                        malformed=malformed,
                    ),
                )
                if (
                    style.get(_W_TYPE) == "paragraph"
                    and style.get(_W_DEFAULT) in ("1", "true", "on")
                    and self._default_style is None
                ):
                    self._default_style = style_id

        numbering = next(_related_parts(document_part, content_types=(CT.WML_NUMBERING,)), None)
        root = _part_element(numbering) if numbering is not None else None
        if root is None:
            return
        for abstract in root.findall(_W_ABSTRACT_NUM):
            abstract_id = abstract.get(_W_ABSTRACT_NUM_ID)
            if abstract_id is None or abstract_id in self._abstracts:
                continue
            link = abstract.find(_W_NUM_STYLE_LINK)
            self._abstracts[abstract_id] = _Abstract(
                levels=_parse_levels(abstract),
                num_style_link=None if link is None else link.get(_W_VAL),
            )
        for num in root.findall(_W_NUM):
            try:
                num_id = int(num.get(_W_NUM_ID_ATTR))
            except (TypeError, ValueError):
                continue
            if num_id in self._nums:
                continue
            abstract_ref = num.find(_W_ABSTRACT_NUM_ID)
            overrides: dict[int, _Override] | None = {}
            try:
                for override in num.findall(_W_LVL_OVERRIDE):
                    ilvl = int(override.get(_W_ILVL))
                    lvl = override.find(_W_LVL)
                    overrides[ilvl] = _Override(
                        start=_int_val(override.find(_W_START_OVERRIDE)),
                        level=None if lvl is None else _parse_level(lvl),
                    )
            except (_Malformed, TypeError, ValueError):
                overrides = None
            self._nums[num_id] = (
                None
                if abstract_ref is None
                else _Num(abstract_id=abstract_ref.get(_W_VAL) or "", overrides=overrides)
            )

    def _view(self, num_id: int) -> _ListLevels:
        """The list a ``w:num`` belongs to, through at most one numbering-style
        link, or :class:`_Unresolved` when the definition is not there."""
        num = self._nums.get(num_id)
        if num is None or num.overrides is None:
            raise _Unresolved(REASON_UNDEFINED)
        abstract_id = num.abstract_id
        abstract = self._abstracts.get(abstract_id)
        if abstract is None:
            raise _Unresolved(REASON_UNDEFINED)
        if abstract.num_style_link:
            style = self._styles.get(abstract.num_style_link)
            target = self._nums.get(style.num_id) if style and style.num_id else None
            linked = self._abstracts.get(target.abstract_id) if target else None
            if linked is None or linked.num_style_link:
                raise _Unresolved(REASON_UNDEFINED)
            abstract_id, abstract = target.abstract_id, linked
        if abstract.levels is None:
            raise _Unresolved(REASON_UNDEFINED)
        return _ListLevels(
            list_id=abstract_id, num_id=num_id, base=abstract.levels, overrides=num.overrides
        )

    def _style_numbering(self, style_id: str | None) -> tuple[int | None, int | None, str | None]:
        """``(numId, ilvl, style that gave the numId)`` from a style chain."""
        num_id = ilvl = None
        source = None
        seen: set[str] = set()
        while style_id and style_id not in seen:
            seen.add(style_id)
            style = self._styles.get(style_id)
            if style is None:
                break
            if style.malformed:
                raise _Unresolved(REASON_UNDEFINED)
            if num_id is None and style.num_id is not None:
                num_id, source = style.num_id, style_id
            if ilvl is None and style.ilvl is not None:
                ilvl = style.ilvl
            style_id = style.based_on
        return num_id, ilvl, source

    def _locate(self, p_el):
        """``(list view, level, style id)`` for a numbered paragraph, ``None``
        for an unnumbered one; :class:`_Unresolved` when it cannot be read."""
        properties = p_el.find(_W_PPR)
        direct_num = direct_ilvl = None
        style_id = self._default_style
        if properties is not None:
            style = properties.find(_W_PSTYLE)
            if style is not None and style.get(_W_VAL):
                style_id = style.get(_W_VAL)
            num_pr = properties.find(_W_NUMPR)
            if num_pr is not None:
                try:
                    direct_num = _int_val(num_pr.find(_W_NUMID))
                    direct_ilvl = _int_val(num_pr.find(_W_ILVL))
                except _Malformed:
                    raise _Unresolved(REASON_UNDEFINED) from None
        style_num, style_ilvl, source_style = self._style_numbering(style_id)
        num_id = direct_num if direct_num is not None else style_num
        if num_id is None or num_id == 0:
            return None
        view = self._view(num_id)
        ilvl = direct_ilvl if direct_ilvl is not None else style_ilvl
        if ilvl is None:
            # A level linked to the paragraph's style (w:lvl/w:pStyle) while
            # the style names no level: Word's "link level to style" and a
            # reading that takes the missing level as 0 part here.
            linked = {
                level.ilvl
                for level in view.base.values()
                if level.style and level.style in (style_id, source_style)
            }
            if linked - {0}:
                raise _Unresolved(REASON_AMBIGUOUS)
            ilvl = 0
        if not 0 <= ilvl <= _MAX_LEVEL:
            raise _Unresolved(REASON_UNDEFINED)
        return view, ilvl, style_id

    # -- walks --------------------------------------------------------------
    def _collect_outside_lists(self, document, body) -> None:
        """The lists numbered paragraphs outside the main text also use."""
        paragraphs = [p for p in body.iter(_W_P) if _in_text_box(p)]
        for part in _related_parts(document.part, reltypes=_HEADER_FOOTER_RELATIONSHIPS):
            root = _part_element(part)
            if root is not None:
                paragraphs.extend(root.iter(_W_P))
        for part in _related_parts(document.part, content_types=_NOTE_CONTENT_TYPES):
            root = _part_element(part)
            if root is not None:
                paragraphs.extend(root.iter(_W_P))
        for p_el in paragraphs:
            try:
                located = self._locate(p_el)
            except _Unresolved:
                continue
            if located is not None:
                self._outside_lists.add(located[0].list_id)

    def _resolve_main_text(self, body) -> None:
        states: dict[str, _ListState] = {}
        for p_el in body.iter(_W_P):
            if _in_text_box(p_el) or _in_unread_branch(p_el) or _mark_deleted(p_el):
                continue
            try:
                located = self._locate(p_el)
            except _Unresolved as exc:
                self._reasons[p_el] = exc.reason
                continue
            if located is None:
                continue
            view, ilvl, _ = located
            state = states.setdefault(view.list_id, _ListState())
            if view.list_id in self._outside_lists:
                state.ambiguous = True
            try:
                self._count(p_el, view, ilvl, state)
            except _Unresolved as exc:
                self._reasons[p_el] = exc.reason

    def _count(self, p_el, view: _ListLevels, ilvl: int, state: _ListState) -> None:
        if state.ambiguous:
            raise _Unresolved(REASON_AMBIGUOUS)
        level = view.level(ilvl)
        if level is None:
            # Word may count a paragraph at a level the list does not define;
            # nothing after it in this list can be numbered without guessing.
            state.ambiguous = True
            raise _Unresolved(REASON_UNDEFINED)
        self._switch_num(view, ilvl, state)
        if ilvl in state.unknown:
            state.ambiguous = True
            raise _Unresolved(REASON_AMBIGUOUS)
        value = state.values[ilvl] + 1 if ilvl in state.values else view.start(ilvl)
        state.values[ilvl] = value
        for shallower in range(ilvl):
            if shallower not in state.values:
                state.unknown.add(shallower)
        for deeper in sorted(set(state.values) | state.unknown):
            if deeper <= ilvl:
                continue
            restarts = _restarts(view.level(deeper), deeper, ilvl)
            if restarts is None:
                state.values.pop(deeper, None)
                state.unknown.add(deeper)
            elif restarts:
                state.values.pop(deeper, None)
                state.unknown.discard(deeper)
        if level.is_bullet:
            return
        label = self._render(view, level, ilvl, state.values)
        if not label:
            return
        separator = "" if level.suffix == "nothing" else " "
        self._labels[p_el] = NumberingLabel(label, separator, view.list_id, ilvl)
        self._contexts[p_el] = (view, level, ilvl, dict(state.values))

    def _switch_num(self, view: _ListLevels, ilvl: int, state: _ListState) -> None:
        """Apply the list-instance rules when a paragraph's ``w:num`` differs
        from the one the list last used (see the module docstring)."""
        num_id = view.num_id
        if state.last_num == num_id:
            return
        overridden = bool(view.overrides)
        if num_id in state.used_nums:
            if state.restarted or overridden:
                state.ambiguous = True
                raise _Unresolved(REASON_AMBIGUOUS)
        elif overridden:
            if not _is_clean_restart(view, ilvl):
                state.ambiguous = True
                raise _Unresolved(REASON_AMBIGUOUS)
            state.values.clear()
            state.unknown.clear()
            state.restarted = True
        elif state.restarted:
            state.ambiguous = True
            raise _Unresolved(REASON_AMBIGUOUS)
        state.used_nums.add(num_id)
        state.last_num = num_id

    def _render(
        self,
        view: _ListLevels,
        level: _Level,
        ilvl: int,
        values: dict[int, int],
        *,
        strict: bool = True,
    ) -> str | None:
        if level.text is None:
            if strict:
                raise _Unresolved(REASON_UNDEFINED)
            return None
        parts: list[str] = []
        cursor = 0
        for match in _PLACEHOLDER_RE.finditer(level.text):
            parts.append(level.text[cursor:match.start()])
            cursor = match.end()
            referenced = int(match.group(1)) - 1
            if not 0 <= referenced <= ilvl:
                if strict:
                    raise _Unresolved(REASON_UNDEFINED)
                return None
            referenced_level = view.level(referenced)
            if referenced_level is None:
                if strict:
                    raise _Unresolved(REASON_UNDEFINED)
                return None
            value = values.get(referenced, view.start(referenced))
            fmt = "decimal" if level.legal else referenced_level.fmt
            written = format_number(value, fmt)
            if written is None:
                if strict:
                    raise _Unresolved(REASON_UNSUPPORTED_FORMAT)
                return None
            parts.append(written)
        parts.append(level.text[cursor:])
        return "".join(parts).strip()

    def _pattern(self, view: _ListLevels, level: _Level, ilvl: int) -> re.Pattern[str] | None:
        text = (level.text or "").strip()
        pieces: list[str] = []
        cursor = 0
        for index, match in enumerate(_PLACEHOLDER_RE.finditer(text)):
            pieces.append(re.escape(text[cursor:match.start()]))
            cursor = match.end()
            referenced = view.level(int(match.group(1)) - 1)
            if referenced is None:
                return None
            shape = _format_shape("decimal" if level.legal else referenced.fmt, index)
            if shape is None:
                return None
            pieces.append(shape)
        pieces.append(re.escape(text[cursor:]))
        body = "".join(pieces)
        if not body:
            return None
        return re.compile(rf"^\s*{body}(?=\s|$)")


def _restarts(level: _Level | None, deeper: int, used: int) -> bool | None:
    """Whether using level ``used`` restarts level ``deeper``.

    ``w:lvlRestart`` absent: yes (any earlier level restarts it). 0: never
    (Word's "Restart list after" left unticked; LibreOffice restarts such a
    level anyway, the standard and Word do not). ``n`` (one-based): yes when
    level ``n`` is the one used, no when a later level is. When a level
    earlier than ``n`` is used the standard does not say, so ``None``. A value
    naming this level or a later one is ignored.
    """
    restart = level.restart if level is not None else None
    if restart is None:
        return True
    if restart == 0:
        return False
    if restart - 1 >= deeper:
        return True
    if used == restart - 1:
        return True
    if used > restart - 1:
        return False
    return None


def _is_clean_restart(view: _ListLevels, ilvl: int) -> bool:
    """A restart both readings agree on: the overriding ``w:num`` starts at
    its first level, overrides that level, leaves every deeper level's start
    alone, and no deeper level is kept from restarting (``w:lvlRestart`` 0)."""
    if ilvl != 0 or 0 not in view.overrides:
        return False
    for deeper in range(1, _MAX_LEVEL + 1):
        base = view.base.get(deeper)
        level = view.level(deeper)
        if deeper in view.overrides and base is not None and view.start(deeper) != base.start:
            return False
        if level is not None and level.restart == 0:
            return False
    return True


def _in_text_box(p_el) -> bool:
    return any(ancestor.tag == _W_TXBX_CONTENT for ancestor in p_el.iterancestors())


def _in_unread_branch(p_el) -> bool:
    """Inside a markup-compatibility branch other than the first (the copy a
    reader skips)."""
    for ancestor in p_el.iterancestors():
        if ancestor.tag not in _MC_BRANCHES:
            continue
        alternate = ancestor.getparent()
        if alternate is None or alternate.tag != _MC_ALTERNATE_CONTENT:
            continue
        first = next((branch for branch in alternate if branch.tag in _MC_BRANCHES), None)
        if first is not ancestor:
            return True
    return False


def _mark_deleted(p_el) -> bool:
    """Whether the paragraph's mark is a tracked deletion (or move source):
    once changes are accepted the paragraph no longer exists."""
    run_properties = p_el.find(f"{_W_PPR}/{_W_RPR}")
    if run_properties is None:
        return False
    return run_properties.find(_W_DEL) is not None or run_properties.find(_W_MOVE_FROM) is not None


def repeats_label(text: str, label: NumberingLabel) -> bool:
    """Whether ``text`` already begins with ``label`` typed out ("1.01 SUMMARY"
    in a paragraph Word also numbers 1.01): the number, then whitespace or
    nothing."""
    typed = text.lstrip()
    if not typed.startswith(label.text):
        return False
    return len(typed) == len(label.text) or typed[len(label.text)].isspace()


def labeled_text(
    label: NumberingLabel | None, text: str
) -> tuple[str, tuple[tuple[int, int], ...]]:
    """A paragraph's ``text`` as Word displays it, and the label's span in it.

    The one rule for the displayed view, shared by the extractor (what the
    review reads) and the applier (what it matches an edit against): the
    label's prefix leads the text, unless the paragraph has no text (it is
    not emitted) or its text already repeats the label (the number is shown
    once, as typed — the typed characters are real text). Without a label the
    text is returned unchanged with no span.
    """
    if label is None or not text.strip() or repeats_label(text, label):
        return text, ()
    return label.prefix + text, ((0, len(label.prefix)),)


def resolve_numbering(document) -> DocumentNumbering:
    """Resolve every automatic number in ``document``'s main text.

    ``document`` is a python-docx ``Document``. Nothing in it is changed:
    parts are found through the document's relationships, never through the
    python-docx accessors that create a missing numbering or styles part.
    """
    return DocumentNumbering(document)


__all__ = [
    "REASON_AMBIGUOUS",
    "REASON_UNDEFINED",
    "REASON_UNSUPPORTED_FORMAT",
    "SUPPORTED_FORMATS",
    "DocumentNumbering",
    "NumberingLabel",
    "format_number",
    "labeled_text",
    "repeats_label",
    "resolve_numbering",
]
