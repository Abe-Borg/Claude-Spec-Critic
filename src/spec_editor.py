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


def _action_confidence(action: EditAction) -> float:
    return action.location.match_confidence


def _severity_rank(action: EditAction) -> int:
    severity = (action.locator_result.finding.severity or "").upper()
    return {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "GRIPES": 3}.get(severity, 99)


def _resolve_overlap_winner(action_a: EditAction, action_b: EditAction) -> EditAction:
    a_range = (action_a.location.match_start, action_a.location.match_end)
    b_range = (action_b.location.match_start, action_b.location.match_end)

    a_contains_b = a_range[0] <= b_range[0] and a_range[1] >= b_range[1]
    b_contains_a = b_range[0] <= a_range[0] and b_range[1] >= a_range[1]
    if a_contains_b and not b_contains_a:
        return action_a
    if b_contains_a and not a_contains_b:
        return action_b

    rank_a = _severity_rank(action_a)
    rank_b = _severity_rank(action_b)
    if rank_a != rank_b:
        return action_a if rank_a < rank_b else action_b

    conf_a = _action_confidence(action_a)
    conf_b = _action_confidence(action_b)
    if conf_a != conf_b:
        return action_a if conf_a > conf_b else action_b

    span_a = a_range[1] - a_range[0]
    span_b = b_range[1] - b_range[0]
    return action_a if span_a >= span_b else action_b


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

        sorted_group = sorted(group, key=lambda item: item.location.match_start, reverse=True)
        accepted: list[EditAction] = []
        for action in sorted_group:
            overlap = None
            for existing in accepted:
                starts_before_end = action.location.match_start < existing.location.match_end
                ends_after_start = action.location.match_end > existing.location.match_start
                if starts_before_end and ends_after_start:
                    overlap = existing
                    break

            if overlap is None:
                accepted.append(action)
                continue

            winner = _resolve_overlap_winner(action, overlap)
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


def _resolve_cell_and_offsets(action: EditAction, row) -> tuple[Paragraph | None, int | None, int | None, str]:
    mapping = action.location.mapping
    row_text_parts: list[tuple[object, str]] = []
    for cell in row.cells:
        text = cell.text.strip()
        if text:
            row_text_parts.append((cell, text))

    if not row_text_parts:
        return None, None, None, "Row had no non-empty cells for mapped text."

    start = action.location.match_start
    end = action.location.match_end
    cursor = 0
    matched_cell = None
    local_start = local_end = None

    for idx, (cell, text) in enumerate(row_text_parts):
        seg_start = cursor
        seg_end = seg_start + len(text)
        if start >= seg_start and end <= seg_end:
            matched_cell = cell
            local_start = start - seg_start
            local_end = end - seg_start
            break
        cursor = seg_end + 3
        if idx == len(row_text_parts) - 1:
            cursor = seg_end

    if matched_cell is None or local_start is None or local_end is None:
        return None, None, None, "Matched range crossed table-cell boundaries; skipping for safety."

    for paragraph in matched_cell.paragraphs:
        idx = paragraph.text.find(action.location.matched_text)
        if idx == -1:
            continue
        return paragraph, idx, idx + len(action.location.matched_text), "Resolved table-cell paragraph target."

    return None, None, None, "Could not resolve target paragraph inside table cell."


def _apply_add_action(action: EditAction, doc: Document) -> EditOutcome:
    mapping = action.location.mapping
    if mapping.element_type != "paragraph":
        return EditOutcome(
            action=action,
            status="failed",
            detail="ADD actions are only supported for paragraph mappings.",
            original_text=action.location.matched_text,
            new_text=None,
        )

    body_children = list(doc.element.body)
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

    anchor_paragraph = Paragraph(anchor_element, doc)
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

    non_add_actions = [action for action in actions_to_apply if action.action_type != "ADD"]
    add_actions = sorted(
        (action for action in actions_to_apply if action.action_type == "ADD"),
        key=lambda item: item.location.mapping.body_index,
        reverse=True,
    )

    body_children = list(doc.element.body)
    for action in non_add_actions:
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

            if action.action_type == "DELETE" and action.location.match_start == 0 and action.location.match_end == len(paragraph_before):
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
                continue

            replacement = action.replacement_text or ""
            ok, detail = _replace_in_paragraph(paragraph, action.location.match_start, action.location.match_end, replacement)
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
            target_paragraph, start, end, detail = _resolve_cell_and_offsets(action, table.rows[row_index])
            if target_paragraph is None or start is None or end is None:
                outcomes.append(
                    EditOutcome(
                        action=action,
                        status="failed",
                        detail=detail,
                        original_text=original_text,
                        new_text=None,
                    )
                )
                continue

            paragraph_before = target_paragraph.text
            if action.action_type == "DELETE" and start == 0 and end == len(paragraph_before):
                ok, replace_detail = _replace_in_paragraph(target_paragraph, start, end, "")
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
            ok, replace_detail = _replace_in_paragraph(target_paragraph, start, end, replacement)
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
        outcomes.append(_apply_add_action(action, doc))

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


def build_edit_actions(locator_results: list[LocatorResult]) -> list[EditAction]:
    actions: list[EditAction] = []
    for finding_index, result in enumerate(locator_results):
        action_type = result.action_type.upper()
        if action_type not in {"EDIT", "DELETE", "ADD"}:
            continue
        if result.status == "not_found" or not result.locations:
            continue

        best_location = max(result.locations, key=lambda location: location.match_confidence)
        if best_location.mapping.element_type in {"header", "footer"}:
            if result.warning is None:
                result.warning = "Header/footer findings are review-only and cannot be auto-applied yet."
            continue
        if result.status == "ambiguous" and result.warning is None:
            result.warning = "Ambiguous locator result; selecting highest-confidence location for auto-apply."

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
