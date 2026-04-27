"""Locate finding edit targets within extracted paragraph mappings."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
import unicodedata

from .extractor import ParagraphMapping
from .reviewer import Finding


_WHITESPACE_RE = re.compile(r"[\s\u00A0]+")
_SECTION_PART_RE = re.compile(r"^\s*part\s+(\d+)\b", flags=re.IGNORECASE)
_SECTION_NUMERIC_RE = re.compile(r"^\s*(\d+(?:\.\d+)+)\b")
_SECTION_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:>|/|\\|→|➜|»)\s*")
_LEADING_NUMBERING_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*[.)-]?\s*|[A-Z][.)-]\s*)+")
_CSI_LEVEL1_HEADINGS = {"general", "products", "execution"}
_UPPERCASE_HEADER_EXCLUSIONS = {"end of section"}


@dataclass
class EditLocation:
    mapping: ParagraphMapping
    match_start: int
    match_end: int
    matched_text: str
    match_confidence: float
    match_method: str


@dataclass
class LocatorResult:
    finding: Finding
    status: str
    locations: list[EditLocation]
    replacement_text: str | None
    action_type: str
    warning: str | None = None


def _resolve_replacement_text(finding: Finding) -> str | None:
    verification = finding.verification
    if verification is None:
        return finding.replacementText
    if verification.verdict == "CORRECTED" and verification.correction:
        return verification.correction
    if verification.verdict in ("CONFIRMED", "UNVERIFIED"):
        return finding.replacementText
    if verification.verdict == "DISPUTED":
        return None
    return finding.replacementText


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text)
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    return normalized.strip().casefold()


def _normalize_with_index_map(text: str) -> tuple[str, list[int]]:
    normalized_chars: list[str] = []
    index_map: list[int] = []
    original = unicodedata.normalize("NFC", text)

    pending_space = False
    for idx, ch in enumerate(original):
        if ch.isspace() or ch == "\u00A0":
            pending_space = True
            continue
        if pending_space and normalized_chars:
            normalized_chars.append(" ")
            index_map.append(idx)
        pending_space = False
        folded = ch.casefold()
        for folded_ch in folded:
            normalized_chars.append(folded_ch)
            index_map.append(idx)

    normalized = "".join(normalized_chars)
    return normalized, index_map


def _confidence_for_count(single: float, multiple: float, count: int, short_text: bool) -> float:
    value = single if count == 1 else multiple
    if short_text:
        value *= 0.75
    return max(0.0, min(1.0, value))


def _exact_match(existing_text: str, paragraph_map: list[ParagraphMapping], *, short_text: bool = False) -> list[EditLocation]:
    hits: list[tuple[ParagraphMapping, int, int, str]] = []
    for mapping in paragraph_map:
        idx = mapping.text.find(existing_text)
        if idx != -1:
            end = idx + len(existing_text)
            hits.append((mapping, idx, end, mapping.text[idx:end]))
            continue
        if mapping.element_type == "table_cell" and " | " in mapping.text:
            cursor = 0
            for segment in mapping.text.split(" | "):
                seg_idx = segment.find(existing_text)
                if seg_idx != -1:
                    start = cursor + seg_idx
                    end = start + len(existing_text)
                    hits.append((mapping, start, end, mapping.text[start:end]))
                    break
                cursor += len(segment) + 3

    confidence = _confidence_for_count(1.0, 0.95, len(hits), short_text)
    return [
        EditLocation(
            mapping=mapping,
            match_start=start,
            match_end=end,
            matched_text=matched,
            match_confidence=confidence,
            match_method="exact",
        )
        for mapping, start, end, matched in hits
    ]


def _normalized_match(existing_text: str, paragraph_map: list[ParagraphMapping], *, short_text: bool = False) -> list[EditLocation]:
    needle_norm = _normalize_text(existing_text)
    if not needle_norm:
        return []

    hits: list[tuple[ParagraphMapping, int, int, str]] = []
    for mapping in paragraph_map:
        normalized_text, index_map = _normalize_with_index_map(mapping.text)
        start_norm = normalized_text.find(needle_norm)
        if start_norm == -1 and mapping.element_type == "table_cell" and " | " in mapping.text:
            cursor = 0
            for segment in mapping.text.split(" | "):
                seg_norm, seg_map = _normalize_with_index_map(segment)
                seg_start = seg_norm.find(needle_norm)
                if seg_start != -1 and seg_map:
                    orig_start = cursor + seg_map[seg_start]
                    end_i = min(seg_start + len(needle_norm) - 1, len(seg_map) - 1)
                    orig_end = cursor + seg_map[end_i] + 1
                    hits.append((mapping, orig_start, orig_end, mapping.text[orig_start:orig_end]))
                    break
                cursor += len(segment) + 3
            continue

        if start_norm == -1 or not index_map:
            continue

        end_norm = start_norm + len(needle_norm) - 1
        if end_norm >= len(index_map):
            continue
        start_orig = index_map[start_norm]
        end_orig = index_map[end_norm] + 1
        hits.append((mapping, start_orig, end_orig, mapping.text[start_orig:end_orig]))

    confidence = _confidence_for_count(0.90, 0.85, len(hits), short_text)
    return [
        EditLocation(
            mapping=mapping,
            match_start=start,
            match_end=end,
            matched_text=matched,
            match_confidence=confidence,
            match_method="normalized",
        )
        for mapping, start, end, matched in hits
    ]


def _fuzzy_match(existing_text: str, paragraph_map: list[ParagraphMapping], threshold: float = 0.80) -> list[EditLocation]:
    hits: list[EditLocation] = []
    for mapping in paragraph_map:
        ratio = SequenceMatcher(None, existing_text, mapping.text).ratio()
        if ratio >= threshold:
            hits.append(
                EditLocation(
                    mapping=mapping,
                    match_start=0,
                    match_end=len(mapping.text),
                    matched_text=mapping.text,
                    match_confidence=ratio,
                    match_method="fuzzy",
                )
            )
    return sorted(hits, key=lambda item: item.match_confidence, reverse=True)


def _extract_section_keys(section: str) -> list[str]:
    section = section.strip()
    if not section:
        return []
    segments = [segment.strip() for segment in _SECTION_SEGMENT_SPLIT_RE.split(section) if segment.strip()]
    if not segments:
        segments = [section]

    def _normalize_segment(segment: str) -> str:
        part_match = _SECTION_PART_RE.match(segment)
        if part_match:
            return f"part {part_match.group(1)}"
        numeric_match = _SECTION_NUMERIC_RE.match(segment)
        if numeric_match:
            return numeric_match.group(1)
        cleaned = _LEADING_NUMBERING_RE.sub("", segment).strip(" -:\t")
        return cleaned.casefold() if cleaned else segment.casefold()

    keys: list[str] = []
    for segment in reversed(segments):
        normalized = _normalize_segment(segment)
        if normalized and normalized not in keys:
            keys.append(normalized)
    if not keys:
        keys.append(section.casefold())
    return keys


def _header_level(text: str) -> int | None:
    text = text.strip()
    part_match = _SECTION_PART_RE.match(text)
    if part_match:
        return 1
    numeric_match = _SECTION_NUMERIC_RE.match(text)
    if numeric_match:
        return len(numeric_match.group(1).split("."))

    cleaned = _LEADING_NUMBERING_RE.sub("", text).strip(" -:\t")
    if not cleaned:
        return None
    if len(cleaned) < 3 or len(cleaned) > 60:
        return None
    if cleaned.casefold() in _UPPERCASE_HEADER_EXCLUSIONS:
        return None
    has_alpha = any(ch.isalpha() for ch in cleaned)
    if not has_alpha or cleaned != cleaned.upper():
        return None
    if cleaned.casefold() in _CSI_LEVEL1_HEADINGS:
        return 1
    return 2


def _section_anchored_match(existing_text: str, section: str, paragraph_map: list[ParagraphMapping], *, short_text: bool = False) -> list[EditLocation]:
    section_keys = _extract_section_keys(section)
    if not section_keys:
        return []

    header_indexes = [idx for idx, mapping in enumerate(paragraph_map) if _header_level(mapping.text) is not None]
    if not header_indexes:
        return []

    anchor_idx = None
    for section_key in section_keys:
        for idx in header_indexes:
            header_text = paragraph_map[idx].text.casefold()
            if section_key in header_text:
                anchor_idx = idx
                break
        if anchor_idx is not None:
            break
    if anchor_idx is None:
        return []

    anchor_level = _header_level(paragraph_map[anchor_idx].text)
    end_idx = len(paragraph_map)
    for idx in header_indexes:
        if idx <= anchor_idx:
            continue
        next_level = _header_level(paragraph_map[idx].text)
        if anchor_level is not None and next_level is not None and next_level <= anchor_level:
            end_idx = idx
            break

    neighborhood = paragraph_map[anchor_idx:end_idx]
    if not neighborhood:
        return []

    for matcher in (
        lambda: _exact_match(existing_text, neighborhood, short_text=short_text),
        lambda: _normalized_match(existing_text, neighborhood, short_text=short_text),
        lambda: _fuzzy_match(existing_text, neighborhood),
    ):
        matches = matcher()
        if matches:
            for location in matches:
                location.match_confidence = 0.70 if short_text else max(0.70, location.match_confidence)
                location.match_method = "section_anchored"
            return matches

    return []


def _cross_paragraph_exact(existing_text: str, paragraph_map: list[ParagraphMapping], *, short_text: bool = False) -> list[list[EditLocation]]:
    if len(paragraph_map) < 2 or "\n\n" not in existing_text:
        return []

    segment_count = len([part for part in existing_text.split("\n\n") if part])
    if segment_count < 2:
        return []

    matches: list[list[EditLocation]] = []
    for start in range(0, len(paragraph_map) - segment_count + 1):
        window = paragraph_map[start : start + segment_count]
        joined = "\n\n".join(m.text for m in window)
        if joined != existing_text:
            continue

        confidence = 0.88 if not short_text else 0.66
        span_locations = [
            EditLocation(
                mapping=mapping,
                match_start=0,
                match_end=len(mapping.text),
                matched_text=mapping.text,
                match_confidence=confidence,
                match_method="exact",
            )
            for mapping in window
        ]
        matches.append(span_locations)
    return matches


def locate_edit(
    finding: Finding,
    paragraph_map: list[ParagraphMapping],
    *,
    min_confidence: float = 0.60,
) -> LocatorResult:
    replacement = _resolve_replacement_text(finding)
    action_type = (finding.actionType or "").strip().upper()
    locator_source = finding.anchorText if action_type == "ADD" else finding.existingText
    existing_text = (locator_source or "").strip()

    if not existing_text:
        return LocatorResult(
            finding=finding,
            status="not_found",
            locations=[],
            replacement_text=replacement,
            action_type=action_type,
            warning="ADD finding has no anchorText; locator cannot determine an insertion point."
            if action_type == "ADD"
            else "Finding has no existingText; locator cannot determine an edit target.",
        )

    if action_type == "ADD" and (finding.insertPosition or "").strip().lower() not in {"before", "after"}:
        return LocatorResult(
            finding=finding,
            status="not_found",
            locations=[],
            replacement_text=replacement,
            action_type=action_type,
            warning="ADD finding has no explicit insertPosition.",
        )

    short_text = len(existing_text) < 15

    match_candidates: list[EditLocation] = []
    methods: list[callable] = []

    if finding.section:
        methods.insert(0, lambda: _section_anchored_match(existing_text, finding.section, paragraph_map, short_text=short_text))

    methods.extend(
        [
            lambda: _exact_match(existing_text, paragraph_map, short_text=short_text),
            lambda: _normalized_match(existing_text, paragraph_map, short_text=short_text),
            lambda: _fuzzy_match(existing_text, paragraph_map),
        ]
    )

    for matcher in methods:
        matches = [m for m in matcher() if m.match_confidence >= min_confidence]
        if matches:
            match_candidates = sorted(matches, key=lambda item: item.match_confidence, reverse=True)
            break

    warning: str | None = None
    if not match_candidates:
        cross_matches = _cross_paragraph_exact(existing_text, paragraph_map, short_text=short_text)
        filtered_spans = [span for span in cross_matches if span and span[0].match_confidence >= min_confidence]
        if filtered_spans:
            warning = "Matched text spans multiple paragraphs; review before auto-applying edit."
            best_span = max(filtered_spans, key=lambda span: span[0].match_confidence)
            return LocatorResult(
                finding=finding,
                status="matched" if len(filtered_spans) == 1 else "ambiguous",
                locations=best_span,
                replacement_text=replacement,
                action_type=action_type,
                warning=warning,
            )
        return LocatorResult(
            finding=finding,
            status="not_found",
            locations=[],
            replacement_text=replacement,
            action_type=action_type,
            warning="No paragraph match met the confidence threshold.",
        )

    status = "matched" if len(match_candidates) == 1 else "ambiguous"
    return LocatorResult(
        finding=finding,
        status=status,
        locations=match_candidates,
        replacement_text=replacement,
        action_type=action_type,
        warning=warning,
    )


def locate_edits(
    findings: list[Finding],
    paragraph_map: list[ParagraphMapping],
    *,
    min_confidence: float = 0.60,
) -> list[LocatorResult]:
    return [locate_edit(finding, paragraph_map, min_confidence=min_confidence) for finding in findings]
