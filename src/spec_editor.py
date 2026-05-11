"""Surgical DOCX edit application utilities."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import re
import unicodedata

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph

from .edit_candidates import (
    SAFETY_AUTO_SAFE,
    SAFETY_AUTO_WITH_CAUTION,
    SAFETY_MANUAL_REVIEW,
    SAFETY_REPORT_ONLY,
)
from .edit_locator import EditLocation, LocatorResult


_WHITESPACE_RE = re.compile(r"[\s\u00A0]+")
_HEADING_HINT_RE = re.compile(r"^\s*(PART\s+\d+|SECTION\s+\d+(\.\d+)*)\b", flags=re.IGNORECASE)


def _normalize_text_for_add(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text)
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    return normalized.strip().casefold()


def _split_insert_paragraphs(text: str) -> list[str]:
    return [chunk.strip() for chunk in re.split(r"\n\s*\n+", text) if chunk.strip()]


def _paragraph_style_id(paragraph_element) -> str | None:
    ppr = paragraph_element.find(qn("w:pPr"))
    if ppr is None:
        return None
    pstyle = ppr.find(qn("w:pStyle"))
    if pstyle is None:
        return None
    return pstyle.get(qn("w:val"))


def _is_heading_like_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    has_alpha = any(ch.isalpha() for ch in stripped)
    is_upper = has_alpha and stripped == stripped.upper()
    short_text = len(stripped) <= 36 and not any(p in stripped for p in ".;:")
    return is_upper or short_text or bool(_HEADING_HINT_RE.match(stripped))


def _reference_style_for_text(anchor_index: int, body_children: list, text: str) -> str | None:
    anchor_style = _paragraph_style_id(body_children[anchor_index])
    if _is_heading_like_text(text):
        return anchor_style

    for idx in range(anchor_index + 1, len(body_children)):
        elem = body_children[idx]
        if not elem.tag.endswith("}p"):
            continue
        para_text = "".join(elem.itertext())
        if _is_heading_like_text(para_text):
            continue
        return _paragraph_style_id(elem) or anchor_style

    return anchor_style


def _build_paragraph_element(text: str, style_id: str | None, anchor_element) -> object:
    paragraph_element = OxmlElement("w:p")
    if style_id:
        ppr = OxmlElement("w:pPr")
        pstyle = OxmlElement("w:pStyle")
        pstyle.set(qn("w:val"), style_id)
        ppr.append(pstyle)
        paragraph_element.append(ppr)
    else:
        anchor_ppr = anchor_element.find(qn("w:pPr"))
        if anchor_ppr is not None:
            paragraph_element.append(deepcopy(anchor_ppr))

    run_element = OxmlElement("w:r")
    text_element = OxmlElement("w:t")
    text_element.text = text
    text_element.set(qn("xml:space"), "preserve")
    run_element.append(text_element)
    paragraph_element.append(run_element)
    return paragraph_element


def _insert_paragraphs_before(anchor_element, texts: list[str], style_id: str | None) -> int:
    inserted = 0
    for text in texts:
        paragraph_element = _build_paragraph_element(text, style_id, anchor_element)
        anchor_element.addprevious(paragraph_element)
        inserted += 1
    return inserted


def _insert_paragraphs_after(anchor_element, texts: list[str], style_id: str | None) -> int:
    inserted = 0
    for text in reversed(texts):
        paragraph_element = _build_paragraph_element(text, style_id, anchor_element)
        anchor_element.addnext(paragraph_element)
        inserted += 1
    return inserted


@dataclass
class EditAction:
    locator_result: LocatorResult
    location: EditLocation
    replacement_text: str | None
    action_type: str
    finding_index: int


@dataclass
class EditOutcome:
    action: EditAction
    status: str
    detail: str
    original_text: str
    new_text: str | None


@dataclass
class EditReport:
    source_path: Path
    output_path: Path
    total_edits_attempted: int
    edits_applied: int
    edits_skipped: int
    edits_failed: int
    outcomes: list[EditOutcome]
    warnings: list[str]


def _build_run_offset_map(paragraph: Paragraph) -> list[tuple[int, int, int]]:
    """Return (run_index, char_start, char_end) offsets for each run in a paragraph."""
    offsets: list[tuple[int, int, int]] = []
    cursor = 0
    for idx, run in enumerate(paragraph.runs):
        start = cursor
        end = start + len(run.text)
        offsets.append((idx, start, end))
        cursor = end
    return offsets


def _replace_in_paragraph(paragraph: Paragraph, match_start: int, match_end: int, replacement: str) -> tuple[bool, str]:
    """Replace text slice [match_start:match_end] in paragraph without removing runs."""
    full_text = paragraph.text
    if match_start < 0 or match_end < match_start or match_end > len(full_text):
        return False, "Invalid match offsets for paragraph text length."

    expected = full_text[:match_start] + replacement + full_text[match_end:]
    if match_start == match_end and not replacement:
        return True, "No-op replacement."

    run_map = _build_run_offset_map(paragraph)
    affected: list[tuple[int, int, int]] = [
        entry for entry in run_map if entry[1] < match_end and entry[2] > match_start
    ]

    if not affected:
        if match_start == match_end:
            if not paragraph.runs:
                paragraph.add_run(replacement)
                return (paragraph.text == expected), "Inserted replacement into empty paragraph."
            first_run = paragraph.runs[0]
            local = max(0, min(len(first_run.text), match_start))
            first_run.text = first_run.text[:local] + replacement + first_run.text[local:]
            return (paragraph.text == expected), "Inserted replacement at run boundary."
        return False, "Could not map target range to paragraph runs."

    first_idx, first_start, _ = affected[0]
    last_idx, last_start, _ = affected[-1]

    first_run = paragraph.runs[first_idx]
    first_prefix_len = max(0, min(len(first_run.text), match_start - first_start))
    first_prefix = first_run.text[:first_prefix_len]

    if first_idx == last_idx:
        suffix_start = max(0, min(len(first_run.text), match_end - first_start))
        suffix = first_run.text[suffix_start:]
        first_run.text = first_prefix + replacement + suffix
    else:
        last_run = paragraph.runs[last_idx]
        suffix_start = max(0, min(len(last_run.text), match_end - last_start))
        suffix = last_run.text[suffix_start:]

        first_run.text = first_prefix + replacement
        for run_idx, _, _ in affected[1:-1]:
            paragraph.runs[run_idx].text = ""
        last_run.text = suffix

    if paragraph.text != expected:
        return False, "Run-level replacement verification failed."
    return True, "Replacement applied successfully."


def _delete_paragraph(paragraph: Paragraph) -> bool:
    element = paragraph._element
    parent = element.getparent()
    if parent is None:
        return False
    parent.remove(element)
    return True


def _is_whole_paragraph_delete(action: EditAction) -> bool:
    location = action.location
    return (
        action.action_type == "DELETE"
        and location.mapping.element_type == "paragraph"
        and location.match_start == 0
        and location.match_end == len(location.mapping.text)
    )


@dataclass(frozen=True)
class PreconditionResult:
    """Outcome of revalidating a recorded edit precondition (Chunk F).

    Carries the offsets that should be used for the actual mutation. If the
    recorded offsets still match the live text, ``match_start`` /
    ``match_end`` are returned unchanged. If the live text shifted but the
    expected text is uniquely present at a different offset, the corrected
    offsets are returned so the caller mutates the right span instead of the
    stale one. ``ok`` is False for missing or duplicated text — the caller
    must skip the edit rather than guess which occurrence to mutate.
    """

    ok: bool
    match_start: int
    match_end: int
    detail: str


def _precondition_holds_for_paragraph(
    paragraph: Paragraph,
    match_start: int,
    match_end: int,
    expected_text: str,
) -> PreconditionResult:
    """Verify the live paragraph still contains the expected slice (audit 8.3, Chunk F).

    Strategy:

    1. If the recorded offsets still slice out the expected text, accept
       and return the recorded offsets unchanged.
    2. Otherwise, if the expected text is uniquely present somewhere else
       in the live paragraph, return ``ok=True`` with the corrected
       offsets so the caller can mutate the actual occurrence rather than
       a stale slice that may now contain unrelated characters.
    3. If the expected text is missing or appears more than once, return
       ``ok=False``. The caller must skip the edit — guessing between
       multiple occurrences risks replacing the wrong span, and silently
       trusting the stale offsets risks corrupting the paragraph.
    """
    current = paragraph.text
    if 0 <= match_start <= match_end <= len(current):
        if current[match_start:match_end] == expected_text:
            return PreconditionResult(
                True,
                match_start,
                match_end,
                "Precondition matched at recorded offsets.",
            )

    if not expected_text:
        return PreconditionResult(
            False,
            match_start,
            match_end,
            "Precondition revalidation failed: paragraph text no longer matches "
            "the recorded edit target and no expected text is recorded.",
        )

    occurrences = current.count(expected_text)
    if occurrences == 1:
        new_start = current.find(expected_text)
        new_end = new_start + len(expected_text)
        return PreconditionResult(
            True,
            new_start,
            new_end,
            "Precondition matched via unique substring presence; offsets corrected "
            f"from [{match_start}, {match_end}) to [{new_start}, {new_end}).",
        )

    if occurrences == 0:
        return PreconditionResult(
            False,
            match_start,
            match_end,
            "Precondition revalidation failed: expected text is no longer present in the paragraph.",
        )

    return PreconditionResult(
        False,
        match_start,
        match_end,
        f"Precondition revalidation failed: expected text appears {occurrences} times "
        "in the paragraph; manual review required to avoid wrong-span replacement.",
    )


def _precondition_holds_for_anchor(anchor_paragraph: Paragraph, expected_text: str) -> tuple[bool, str]:
    if not expected_text:
        return True, "No anchor precondition recorded."
    if expected_text in anchor_paragraph.text or anchor_paragraph.text in expected_text:
        return True, "Anchor precondition holds."
    return (
        False,
        "Precondition revalidation failed: anchor paragraph text no longer matches the recorded anchor.",
    )


def _action_confidence(action: EditAction) -> float:
    return action.location.match_confidence


def _severity_rank(action: EditAction) -> int:
    severity = (action.locator_result.finding.severity or "").upper()
    return {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "GRIPES": 3}.get(severity, 99)


def _resolve_overlap_winner(action_a: EditAction, action_b: EditAction) -> EditAction | None:
    """Pick a clear winner for two overlapping edits, or return None if ambiguous.

    Chunk D3.1: We previously fell through to severity / confidence / span
    heuristics when neither edit strictly contained the other. That silently
    picked a winner for genuinely ambiguous partial overlaps and discarded
    the loser's intent. Per the delta plan, ambiguous overlapping edits
    should be flagged for manual review rather than misapplied.

    Resolution rules:

    * Strict containment (one edit's span is fully inside the other and they
      are not identical): the broader edit wins. The narrower edit's intent
      is included in the broader edit's replacement span, so applying the
      broader edit is not a misapplication.
    * Identical span: fall back to severity / confidence / span tie-breakers
      so duplicate findings collapse to a single applied edit rather than
      both being thrown away.
    * Partial overlap (neither strictly contains the other): return ``None``.
      The caller skips both edits with a manual-review detail; auto-applying
      either one would discard the other's distinct intent.
    """
    a_range = (action_a.location.match_start, action_a.location.match_end)
    b_range = (action_b.location.match_start, action_b.location.match_end)

    a_contains_b = a_range[0] <= b_range[0] and a_range[1] >= b_range[1]
    b_contains_a = b_range[0] <= a_range[0] and b_range[1] >= a_range[1]

    if a_contains_b and b_contains_a:
        # Identical spans — duplicate or near-duplicate findings. Keep the
        # heuristic tie-breaker so dedup-survivors still collapse to one
        # applied edit rather than both being lost to "ambiguous".
        rank_a = _severity_rank(action_a)
        rank_b = _severity_rank(action_b)
        if rank_a != rank_b:
            return action_a if rank_a < rank_b else action_b

        conf_a = _action_confidence(action_a)
        conf_b = _action_confidence(action_b)
        if conf_a != conf_b:
            return action_a if conf_a > conf_b else action_b

        return action_a

    if a_contains_b and not b_contains_a:
        return action_a
    if b_contains_a and not a_contains_b:
        return action_b

    # Partial overlap with no containment is ambiguous. Caller must skip both.
    return None


def _action_group_key(action: EditAction) -> tuple[int, str, int | None]:
    mapping = action.location.mapping
    return mapping.body_index, mapping.element_type, mapping.row_index


def _detect_and_resolve_conflicts(actions: list[EditAction]) -> tuple[list[EditAction], list[EditOutcome]]:
    grouped: dict[tuple[int, str, int | None], list[EditAction]] = defaultdict(list)
    for action in actions:
        grouped[_action_group_key(action)].append(action)

    to_apply: list[EditAction] = []
    skipped: list[EditOutcome] = []

    for group in grouped.values():
        if len(group) == 1:
            to_apply.extend(group)
            continue

        whole_deletes = [action for action in group if _is_whole_paragraph_delete(action)]
        if whole_deletes:
            winner = max(whole_deletes, key=_action_confidence)
            to_apply.append(winner)
            for action in group:
                if action is winner:
                    continue
                skipped.append(
                    EditOutcome(
                        action=action,
                        status="skipped",
                        detail="Skipped due to whole-paragraph DELETE conflict in same target.",
                        original_text=action.location.matched_text,
                        new_text=None,
                    )
                )
            continue

        # Chunk D3.1: process the group in descending-start order so the
        # higher-offset edit is checked first. Track tainted ranges from
        # ambiguous-overlap resolutions so a third edit overlapping with
        # either side of an already-discarded ambiguous pair is also
        # skipped (rather than slipping through because the original
        # conflicting actions were removed from ``accepted``).
        sorted_group = sorted(group, key=lambda item: item.location.match_start, reverse=True)
        accepted: list[EditAction] = []
        ambiguous_ranges: list[tuple[int, int]] = []

        def _spans_overlap(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
            return start_a < end_b and end_a > start_b

        for action in sorted_group:
            a_start = action.location.match_start
            a_end = action.location.match_end

            # If this action overlaps any previously-tainted region, it is
            # also ambiguous: the original conflicting pair is gone from
            # ``accepted`` but their span is still untrustworthy.
            tainted = next(
                (
                    (start, end)
                    for start, end in ambiguous_ranges
                    if _spans_overlap(a_start, a_end, start, end)
                ),
                None,
            )
            if tainted is not None:
                skipped.append(
                    EditOutcome(
                        action=action,
                        status="skipped",
                        detail=(
                            "Skipped: overlaps a same-paragraph region already "
                            "flagged for manual review due to ambiguous "
                            f"conflicting edits [{tainted[0]}, {tainted[1]})."
                        ),
                        original_text=action.location.matched_text,
                        new_text=None,
                    )
                )
                continue

            overlap = None
            for existing in accepted:
                if _spans_overlap(
                    a_start,
                    a_end,
                    existing.location.match_start,
                    existing.location.match_end,
                ):
                    overlap = existing
                    break

            if overlap is None:
                accepted.append(action)
                continue

            winner = _resolve_overlap_winner(action, overlap)
            if winner is None:
                # Chunk D3.1: ambiguous partial overlap — skip both edits and
                # taint the union range so any further action overlapping
                # this region is also routed to manual review.
                accepted.remove(overlap)
                detail = (
                    "Skipped due to ambiguous overlapping edits in the same "
                    "paragraph; manual review required to avoid silently "
                    "picking one intent over the other."
                )
                skipped.append(
                    EditOutcome(
                        action=overlap,
                        status="skipped",
                        detail=detail,
                        original_text=overlap.location.matched_text,
                        new_text=None,
                    )
                )
                skipped.append(
                    EditOutcome(
                        action=action,
                        status="skipped",
                        detail=detail,
                        original_text=action.location.matched_text,
                        new_text=None,
                    )
                )
                ambiguous_ranges.append(
                    (
                        min(a_start, overlap.location.match_start),
                        max(a_end, overlap.location.match_end),
                    )
                )
                continue

            if winner is action:
                accepted.remove(overlap)
                skipped.append(
                    EditOutcome(
                        action=overlap,
                        status="skipped",
                        detail="Skipped due to overlapping conflict with broader/higher-priority edit.",
                        original_text=overlap.location.matched_text,
                        new_text=None,
                    )
                )
                accepted.append(action)
            else:
                skipped.append(
                    EditOutcome(
                        action=action,
                        status="skipped",
                        detail="Skipped due to overlapping conflict with broader/higher-priority edit.",
                        original_text=action.location.matched_text,
                        new_text=None,
                    )
                )

        to_apply.extend(sorted(accepted, key=lambda item: item.location.match_start, reverse=True))

    ordered = sorted(
        to_apply,
        key=lambda item: (
            item.location.mapping.body_index,
            -item.location.match_start,
        ),
    )
    return ordered, skipped


def _resolve_cell_and_offsets(
    action: EditAction, row
) -> tuple[Paragraph | None, int | None, int | None, str, str]:
    """Locate the target paragraph and span for a table-cell edit.

    Returns ``(paragraph, start, end, detail, status)`` where ``status`` is
    one of ``"resolved"``, ``"failed"`` (data shape problem the caller
    should treat as failed), or ``"skipped"`` (a deliberate Chunk F safety
    refusal — duplicated or missing target text). The caller uses
    ``status`` to record the right outcome.
    """
    mapping = action.location.mapping
    row_text_parts: list[tuple[object, str]] = []
    for cell in row.cells:
        text = cell.text.strip()
        if text:
            row_text_parts.append((cell, text))

    if not row_text_parts:
        return None, None, None, "Row had no non-empty cells for mapped text.", "failed"

    start = action.location.match_start
    end = action.location.match_end
    cursor = 0
    matched_cell = None

    for idx, (cell, text) in enumerate(row_text_parts):
        seg_start = cursor
        seg_end = seg_start + len(text)
        if start >= seg_start and end <= seg_end:
            matched_cell = cell
            break
        cursor = seg_end + 3
        if idx == len(row_text_parts) - 1:
            cursor = seg_end

    if matched_cell is None:
        return (
            None,
            None,
            None,
            "Matched range crossed table-cell boundaries; skipping for safety.",
            "failed",
        )

    # Chunk F: enumerate every occurrence of the expected text across the
    # cell's paragraphs and require uniqueness. The previous implementation
    # picked the first ``paragraph.text.find()`` hit, which silently guessed
    # when the text appeared multiple times in the cell.
    expected = action.location.matched_text
    if not expected:
        return None, None, None, "No expected text recorded for table-cell edit.", "failed"

    candidates: list[tuple[Paragraph, int]] = []
    for paragraph in matched_cell.paragraphs:
        text = paragraph.text
        scan = 0
        while True:
            hit = text.find(expected, scan)
            if hit == -1:
                break
            candidates.append((paragraph, hit))
            scan = hit + 1

    if not candidates:
        return (
            None,
            None,
            None,
            "Expected text is no longer present in any paragraph of the target cell.",
            "skipped",
        )
    if len(candidates) > 1:
        return (
            None,
            None,
            None,
            (
                f"Expected text appears {len(candidates)} times across this cell's "
                "paragraphs; manual review required to avoid wrong-span replacement."
            ),
            "skipped",
        )

    paragraph, hit = candidates[0]
    return paragraph, hit, hit + len(expected), "Resolved table-cell paragraph target.", "resolved"


def _apply_add_action(
    action: EditAction,
    doc: Document,
    *,
    original_body_children: list | None = None,
) -> EditOutcome:
    mapping = action.location.mapping
    if mapping.element_type != "paragraph":
        return EditOutcome(
            action=action,
            status="failed",
            detail="ADD actions are only supported for paragraph mappings.",
            original_text=action.location.matched_text,
            new_text=None,
        )

    # Use the pre-mutation snapshot so DELETE actions earlier in the same
    # apply pass do not shift the body_index used by ADD (audit Issue 6).
    body_children = (
        list(original_body_children)
        if original_body_children is not None
        else list(doc.element.body)
    )
    if mapping.body_index < 0 or mapping.body_index >= len(body_children):
        return EditOutcome(
            action=action,
            status="failed",
            detail="Body index is out of range in current document.",
            original_text=action.location.matched_text,
            new_text=None,
        )

    anchor_element = body_children[mapping.body_index]
    if not anchor_element.tag.endswith("}p"):
        return EditOutcome(
            action=action,
            status="failed",
            detail="ADD mapping expected paragraph but body element was not paragraph.",
            original_text=action.location.matched_text,
            new_text=None,
        )

    # If the anchor was already removed by a DELETE earlier in this run,
    # there is no longer a parent to insert beside. Fail safely instead of
    # silently writing into an orphaned XML node.
    if anchor_element.getparent() is None:
        return EditOutcome(
            action=action,
            status="failed",
            detail="ADD anchor paragraph was deleted earlier in this edit pass; skip.",
            original_text=action.location.matched_text,
            new_text=None,
        )

    anchor_paragraph = Paragraph(anchor_element, doc)

    # Phase 4 (audit Section 8.3): revalidate anchor before mutating. If a
    # prior edit changed the anchor paragraph text in a way that no longer
    # matches the recorded anchor, do not insert beside it.
    ok_anchor, anchor_detail = _precondition_holds_for_anchor(
        anchor_paragraph,
        action.location.matched_text,
    )
    if not ok_anchor:
        return EditOutcome(
            action=action,
            status="skipped",
            detail=anchor_detail,
            original_text=anchor_paragraph.text,
            new_text=None,
        )

    replacement = (action.replacement_text or "").strip()
    if not replacement:
        return EditOutcome(
            action=action,
            status="skipped",
            detail="ADD action had empty replacement text; nothing to insert.",
            original_text=anchor_paragraph.text,
            new_text=None,
        )

    anchor_text = action.location.matched_text or anchor_paragraph.text
    replacement_norm = _normalize_text_for_add(replacement)
    anchor_norm = _normalize_text_for_add(anchor_text)

    # If the model provided an explicit insertPosition, trust it instead of
    # guessing from text overlap (audit Issue 5). This makes ADD edits
    # deterministic when the prompt produced the structured anchor model.
    explicit_position = (
        getattr(action.locator_result.finding, "insertPosition", None) or ""
    ).strip().lower()

    if explicit_position in {"before", "after"}:
        position = explicit_position
        new_content = replacement
    else:
        position = "replace"
        new_content = replacement
        if anchor_norm and replacement_norm.startswith(anchor_norm):
            position = "after"
            new_content = replacement[len(anchor_text):].strip()
        elif anchor_norm and replacement_norm.endswith(anchor_norm):
            position = "before"
            new_content = replacement[: max(0, len(replacement) - len(anchor_text))].strip()
        elif anchor_norm and anchor_norm not in replacement_norm:
            position = "after"
            new_content = replacement

    if position == "replace":
        ok, detail = _replace_in_paragraph(
            anchor_paragraph,
            action.location.match_start,
            action.location.match_end,
            replacement,
        )
        return EditOutcome(
            action=action,
            status="applied" if ok else "failed",
            detail=f"ADD fallback to replace: {detail}",
            original_text=anchor_text,
            new_text=anchor_paragraph.text if ok else None,
        )

    paragraphs = _split_insert_paragraphs(new_content)
    if not paragraphs:
        return EditOutcome(
            action=action,
            status="skipped",
            detail="ADD replacement contained no additional content beyond anchor.",
            original_text=anchor_paragraph.text,
            new_text=None,
        )

    style_id = _reference_style_for_text(mapping.body_index, body_children, paragraphs[0])
    inserted_count = (
        _insert_paragraphs_after(anchor_element, paragraphs, style_id)
        if position == "after"
        else _insert_paragraphs_before(anchor_element, paragraphs, style_id)
    )
    return EditOutcome(
        action=action,
        status="applied",
        detail=f"Inserted {inserted_count} paragraph(s) {position} anchor paragraph.",
        original_text=anchor_paragraph.text,
        new_text="\n\n".join(paragraphs),
    )


def apply_edits_to_spec(source_path: Path, output_path: Path, edit_actions: list[EditAction]) -> EditReport:
    source_path = Path(source_path)
    output_path = Path(output_path)

    if source_path.resolve() == output_path.resolve():
        raise ValueError("output_path must differ from source_path; refusing to overwrite source document.")

    doc = Document(source_path)
    actions_to_apply, pre_skipped = _detect_and_resolve_conflicts(edit_actions)
    outcomes: list[EditOutcome] = list(pre_skipped)
    warnings: list[str] = []

    for skipped in pre_skipped:
        warnings.append(skipped.detail)

    # Phase 4 (audit Section 8.4): apply edits in a deterministic safety
    # order — in-place replacements first, then ADDs, then whole-paragraph
    # DELETEs in descending body_index. This keeps anchor elements live for
    # ADDs and avoids any ordering surprise where a DELETE shifts the
    # document structure before later edits run.
    replacement_actions: list[EditAction] = []
    whole_delete_actions: list[EditAction] = []
    for action in actions_to_apply:
        if action.action_type == "ADD":
            continue
        if _is_whole_paragraph_delete(action):
            whole_delete_actions.append(action)
        else:
            replacement_actions.append(action)

    add_actions = sorted(
        (action for action in actions_to_apply if action.action_type == "ADD"),
        key=lambda item: item.location.mapping.body_index,
        reverse=True,
    )
    whole_delete_actions.sort(
        key=lambda item: item.location.mapping.body_index,
        reverse=True,
    )

    body_children = list(doc.element.body)
    for action in replacement_actions:
        mapping = action.location.mapping
        original_text = action.location.matched_text

        if mapping.body_index < 0 or mapping.body_index >= len(body_children):
            outcomes.append(
                EditOutcome(
                    action=action,
                    status="failed",
                    detail="Body index is out of range in current document.",
                    original_text=original_text,
                    new_text=None,
                )
            )
            continue

        element = body_children[mapping.body_index]

        if mapping.element_type == "paragraph":
            if not element.tag.endswith("}p"):
                outcomes.append(
                    EditOutcome(
                        action=action,
                        status="failed",
                        detail="Mapping expected paragraph but body element was not paragraph.",
                        original_text=original_text,
                        new_text=None,
                    )
                )
                continue
            paragraph = Paragraph(element, doc)
            paragraph_before = paragraph.text

            # Phase 4 (audit Section 8.3) + Chunk F: revalidate immediately
            # before mutating. If a previous edit in this pass changed the
            # paragraph such that the recorded slice no longer matches, the
            # precondition returns corrected offsets when the expected text
            # is uniquely present elsewhere; if it is missing or duplicated
            # we skip the edit instead of replacing a stale span.
            precondition = _precondition_holds_for_paragraph(
                paragraph,
                action.location.match_start,
                action.location.match_end,
                action.location.matched_text,
            )
            if not precondition.ok:
                outcomes.append(
                    EditOutcome(
                        action=action,
                        status="skipped",
                        detail=precondition.detail,
                        original_text=paragraph_before,
                        new_text=None,
                    )
                )
                continue

            replacement = action.replacement_text or ""
            ok, detail = _replace_in_paragraph(
                paragraph,
                precondition.match_start,
                precondition.match_end,
                replacement,
            )
            outcomes.append(
                EditOutcome(
                    action=action,
                    status="applied" if ok else "failed",
                    detail=detail,
                    original_text=paragraph_before,
                    new_text=paragraph.text if ok else None,
                )
            )
            continue

        if mapping.element_type == "table_cell":
            if not element.tag.endswith("}tbl"):
                outcomes.append(
                    EditOutcome(
                        action=action,
                        status="failed",
                        detail="Mapping expected table but body element was not table.",
                        original_text=original_text,
                        new_text=None,
                    )
                )
                continue
            table = DocxTable(element, doc)
            row_index = mapping.row_index
            if row_index is None or row_index < 0 or row_index >= len(table.rows):
                outcomes.append(
                    EditOutcome(
                        action=action,
                        status="failed",
                        detail="Row index for table-cell mapping was invalid.",
                        original_text=original_text,
                        new_text=None,
                    )
                )
                continue
            target_paragraph, start, end, detail, resolve_status = _resolve_cell_and_offsets(
                action, table.rows[row_index]
            )
            if target_paragraph is None or start is None or end is None:
                outcomes.append(
                    EditOutcome(
                        action=action,
                        status="skipped" if resolve_status == "skipped" else "failed",
                        detail=detail,
                        original_text=original_text,
                        new_text=None,
                    )
                )
                continue

            paragraph_before = target_paragraph.text

            # Chunk F: same offset-safety contract as the paragraph path.
            # The table-cell resolver finds the expected text by substring
            # search, but a prior edit in this pass could have shifted or
            # duplicated it; revalidate and use the precondition's
            # (possibly corrected) offsets for the actual replacement.
            precondition = _precondition_holds_for_paragraph(
                target_paragraph,
                start,
                end,
                action.location.matched_text,
            )
            if not precondition.ok:
                outcomes.append(
                    EditOutcome(
                        action=action,
                        status="skipped",
                        detail=precondition.detail,
                        original_text=paragraph_before,
                        new_text=None,
                    )
                )
                continue

            cell_start = precondition.match_start
            cell_end = precondition.match_end

            if action.action_type == "DELETE" and cell_start == 0 and cell_end == len(paragraph_before):
                ok, replace_detail = _replace_in_paragraph(target_paragraph, cell_start, cell_end, "")
                outcomes.append(
                    EditOutcome(
                        action=action,
                        status="applied" if ok else "failed",
                        detail=replace_detail,
                        original_text=paragraph_before,
                        new_text=target_paragraph.text if ok else None,
                    )
                )
                continue

            replacement = action.replacement_text or ""
            ok, replace_detail = _replace_in_paragraph(target_paragraph, cell_start, cell_end, replacement)
            outcomes.append(
                EditOutcome(
                    action=action,
                    status="applied" if ok else "failed",
                    detail=replace_detail,
                    original_text=paragraph_before,
                    new_text=target_paragraph.text if ok else None,
                )
            )
            continue

        outcomes.append(
            EditOutcome(
                action=action,
                status="skipped",
                detail=f"Unsupported mapping element type: {mapping.element_type}",
                original_text=original_text,
                new_text=None,
            )
        )

    for action in add_actions:
        outcomes.append(
            _apply_add_action(action, doc, original_body_children=body_children)
        )

    # Whole-paragraph DELETEs run last (audit Section 8.4). Descending body
    # order keeps the snapshot indices stable and avoids any chance that a
    # remove() upstream of an ADD anchor could orphan that anchor before the
    # ADD applied.
    for action in whole_delete_actions:
        mapping = action.location.mapping
        if mapping.body_index < 0 or mapping.body_index >= len(body_children):
            outcomes.append(
                EditOutcome(
                    action=action,
                    status="failed",
                    detail="Body index is out of range in current document.",
                    original_text=action.location.matched_text,
                    new_text=None,
                )
            )
            continue
        element = body_children[mapping.body_index]
        if not element.tag.endswith("}p"):
            outcomes.append(
                EditOutcome(
                    action=action,
                    status="failed",
                    detail="Mapping expected paragraph but body element was not paragraph.",
                    original_text=action.location.matched_text,
                    new_text=None,
                )
            )
            continue
        if element.getparent() is None:
            outcomes.append(
                EditOutcome(
                    action=action,
                    status="skipped",
                    detail="Paragraph already removed by an earlier edit; skipping DELETE.",
                    original_text=action.location.matched_text,
                    new_text=None,
                )
            )
            continue
        paragraph = Paragraph(element, doc)
        paragraph_before = paragraph.text

        precondition = _precondition_holds_for_paragraph(
            paragraph,
            action.location.match_start,
            action.location.match_end,
            action.location.matched_text,
        )
        if not precondition.ok:
            outcomes.append(
                EditOutcome(
                    action=action,
                    status="skipped",
                    detail=precondition.detail,
                    original_text=paragraph_before,
                    new_text=None,
                )
            )
            continue

        ok = _delete_paragraph(paragraph)
        outcomes.append(
            EditOutcome(
                action=action,
                status="applied" if ok else "failed",
                detail="Deleted full paragraph." if ok else "Failed to delete paragraph element.",
                original_text=paragraph_before,
                new_text="" if ok else None,
            )
        )

    try:
        doc.save(BytesIO())
    except Exception as exc:
        failed_outcomes = [
            EditOutcome(
                action=outcome.action,
                status="failed",
                detail=f"Document serialization failed after edits: {exc}",
                original_text=outcome.original_text,
                new_text=None,
            )
            for outcome in outcomes
        ]
        return EditReport(
            source_path=source_path,
            output_path=output_path,
            total_edits_attempted=len(edit_actions),
            edits_applied=0,
            edits_skipped=0,
            edits_failed=len(failed_outcomes),
            outcomes=failed_outcomes,
            warnings=warnings + [f"Serialization check failed: {exc}"],
        )

    doc.save(output_path)

    applied = sum(1 for outcome in outcomes if outcome.status == "applied")
    skipped_count = sum(1 for outcome in outcomes if outcome.status == "skipped")
    failed = sum(1 for outcome in outcomes if outcome.status == "failed")

    return EditReport(
        source_path=source_path,
        output_path=output_path,
        total_edits_attempted=len(edit_actions),
        edits_applied=applied,
        edits_skipped=skipped_count,
        edits_failed=failed,
        outcomes=outcomes,
        warnings=warnings,
    )


def build_edit_actions(
    locator_results: list[LocatorResult],
    *,
    allow_caution: bool = True,
) -> list[EditAction]:
    """Convert locator results into mutating edit actions.

    Phase 4: gate auto-application on locator-level safety categories. Only
    AUTO_SAFE results are accepted by default; AUTO_WITH_CAUTION is included
    when allow_caution is True (preserves existing behavior). MANUAL_REVIEW
    and REPORT_ONLY locator results never produce actions.
    """
    actions: list[EditAction] = []
    for finding_index, result in enumerate(locator_results):
        action_type = result.action_type.upper()
        if action_type not in {"EDIT", "DELETE", "ADD"}:
            continue
        if result.status == "not_found" or not result.locations:
            continue

        # Ambiguous locator results have multiple plausible targets; previous
        # behavior silently picked the highest-confidence candidate, which
        # could mutate the wrong paragraph. Per audit Issue 4, ambiguous
        # matches must be manual-review-only.
        if result.status == "ambiguous":
            result.warning = (
                "Ambiguous locator result; multiple targets matched. "
                "Review and apply manually instead of auto-editing."
            )
            continue

        category = (result.safety_category or "").upper()
        if category == SAFETY_MANUAL_REVIEW:
            if result.warning is None:
                result.warning = (
                    "Locator classified as manual review; not eligible for auto-apply."
                )
            continue
        if category == SAFETY_REPORT_ONLY:
            continue
        if category == SAFETY_AUTO_WITH_CAUTION and not allow_caution:
            if result.warning is None:
                result.warning = (
                    "Locator classified AUTO_WITH_CAUTION; auto-apply suppressed by caller."
                )
            continue

        best_location = max(result.locations, key=lambda location: location.match_confidence)
        if best_location.mapping.element_type in {"header", "footer"}:
            if result.warning is None:
                result.warning = "Header/footer findings are review-only and cannot be auto-applied yet."
            continue

        actions.append(
            EditAction(
                locator_result=result,
                location=best_location,
                replacement_text=result.replacement_text,
                action_type=action_type,
                finding_index=finding_index,
            )
        )
    return actions


def apply_edits_to_specs(edit_plan: list[tuple[Path, Path, list[EditAction]]]) -> list[EditReport]:
    return [apply_edits_to_spec(source, output, actions) for source, output, actions in edit_plan]


# ---------------------------------------------------------------------------
# Phase 4.6 (audit Section 8.6): change-log / annotation mode.
#
# An optional alternative to mutating the spec text. Instead of rewriting
# paragraphs, this writes a copy of the document with a yellow-highlighted
# annotation paragraph inserted immediately after each target anchor:
#
#     [SPEC CRITIC SUGGESTION • EDIT — Section 23 05 23 §2.3.A]
#       Existing: <original verbatim>
#       Proposed: <suggested verbatim>
#       Issue:    <one-line summary>
#
# This is the safe option for table cells, header/footer text, richly
# formatted paragraphs, and ambiguous matches — the original text is never
# touched, and the user can review the inline suggestions in Word and
# accept/reject by hand.
# ---------------------------------------------------------------------------


def _annotation_label(action: EditAction) -> str:
    finding = getattr(action.locator_result, "finding", None)
    section = (getattr(finding, "section", "") or "").strip()
    section_part = f" — Section {section}" if section else ""
    return f"[SPEC CRITIC SUGGESTION • {action.action_type}{section_part}]"


def _insert_annotation_paragraph(anchor_paragraph: Paragraph, lines: list[str]) -> None:
    """Insert a yellow-highlighted annotation paragraph after ``anchor_paragraph``.

    The annotation is a single paragraph with the lines joined by ``\\n``
    (rendered as soft line breaks) so the suggestion stays grouped in one
    block in Word. Highlighting uses ``w:highlight w:val="yellow"`` so the
    user sees the annotation distinctly.
    """
    new_para = OxmlElement("w:p")
    pPr = OxmlElement("w:pPr")
    new_para.append(pPr)
    for i, line in enumerate(lines):
        if i > 0:
            br_run = OxmlElement("w:r")
            br = OxmlElement("w:br")
            br_run.append(br)
            new_para.append(br_run)
        run = OxmlElement("w:r")
        rPr = OxmlElement("w:rPr")
        highlight = OxmlElement("w:highlight")
        highlight.set(qn("w:val"), "yellow")
        rPr.append(highlight)
        # Bold the first line (the label).
        if i == 0:
            b = OxmlElement("w:b")
            rPr.append(b)
        run.append(rPr)
        text_el = OxmlElement("w:t")
        text_el.text = line
        text_el.set(qn("xml:space"), "preserve")
        run.append(text_el)
        new_para.append(run)
    anchor_paragraph._element.addnext(new_para)


def _annotation_lines_for_action(action: EditAction) -> list[str]:
    label = _annotation_label(action)
    lines = [label]
    original = (action.location.matched_text or "").strip()
    proposed = (action.replacement_text or "").strip()
    if action.action_type == "EDIT":
        if original:
            lines.append(f"  Existing: {original[:600]}")
        if proposed:
            lines.append(f"  Proposed: {proposed[:600]}")
    elif action.action_type == "ADD":
        if proposed:
            lines.append(f"  Insert {action.locator_result.warning or ''}".rstrip())
            lines.append(f"  Proposed: {proposed[:600]}")
    elif action.action_type == "DELETE":
        if original:
            lines.append(f"  Delete: {original[:600]}")
    warning = (action.locator_result.warning or "").strip()
    if warning:
        lines.append(f"  Note: {warning[:300]}")
    return lines


def annotate_spec_with_suggestions(
    source_path: Path,
    output_path: Path,
    edit_actions: list[EditAction],
) -> EditReport:
    """Write a copy of the spec with inline yellow-highlighted suggestions.

    Phase 4.6: this mode never mutates the original spec text — it only
    inserts annotation paragraphs after each anchor. Useful for table
    cells, header/footer text, and richly formatted paragraphs where a
    direct rewrite would be unsafe.
    """
    source_path = Path(source_path)
    output_path = Path(output_path)
    if source_path.resolve() == output_path.resolve():
        raise ValueError("output_path must differ from source_path; refusing to overwrite source document.")

    doc = Document(source_path)
    body_children = list(doc.element.body)
    outcomes: list[EditOutcome] = []
    annotated = 0
    skipped = 0
    failed = 0

    # Process in descending body_index so each insertion does not shift the
    # indices of earlier targets.
    sorted_actions = sorted(
        edit_actions,
        key=lambda a: a.location.mapping.body_index,
        reverse=True,
    )
    for action in sorted_actions:
        mapping = action.location.mapping
        if mapping.body_index < 0 or mapping.body_index >= len(body_children):
            failed += 1
            outcomes.append(EditOutcome(
                action=action, status="failed",
                detail="Anchor index out of range; cannot annotate.",
                original_text=mapping.text, new_text=None,
            ))
            continue
        element = body_children[mapping.body_index]
        if element.tag != qn("w:p"):
            # Skip table-cell / non-paragraph anchors — the simple
            # addnext insertion does not work cleanly for cells.
            skipped += 1
            outcomes.append(EditOutcome(
                action=action, status="skipped",
                detail="Annotation mode currently supports paragraph anchors only.",
                original_text=mapping.text, new_text=None,
            ))
            continue
        anchor = Paragraph(element, doc.element.body)
        try:
            _insert_annotation_paragraph(anchor, _annotation_lines_for_action(action))
            annotated += 1
            outcomes.append(EditOutcome(
                action=action, status="applied",
                detail="Annotation inserted; original text unchanged.",
                original_text=mapping.text,
                new_text=action.replacement_text,
            ))
        except Exception as exc:  # pragma: no cover - defensive
            failed += 1
            outcomes.append(EditOutcome(
                action=action, status="failed",
                detail=f"Annotation insertion raised: {exc}",
                original_text=mapping.text, new_text=None,
            ))

    # Validate the document still serializes before writing to disk.
    buf = BytesIO()
    doc.save(buf)
    buf.seek(0)
    output_path.write_bytes(buf.read())

    return EditReport(
        source_path=source_path,
        output_path=output_path,
        total_edits_attempted=len(edit_actions),
        edits_applied=annotated,
        edits_skipped=skipped,
        edits_failed=failed,
        outcomes=outcomes,
        warnings=[],
    )
