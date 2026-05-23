"""Locate finding edit targets within extracted paragraph mappings."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
import unicodedata

from .edit_candidates import (
    SAFETY_AUTO_SAFE,
    SAFETY_AUTO_WITH_CAUTION,
    SAFETY_MANUAL_REVIEW,
    SAFETY_REPORT_ONLY,
)
from .replacement_style import (
    correction_looks_replaceable,
    use_verifier_correction_as_replacement_enabled,
)
from ..input.extractor import ParagraphMapping
from ..review.reviewer import Finding


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
    # Paragraph-locator safety category. Values: AUTO_SAFE,
    # AUTO_WITH_CAUTION, MANUAL_REVIEW, REPORT_ONLY. Computed from match
    # method/confidence, ambiguity, element type, and structural span.
    # build_edit_actions uses this to gate auto-application. Left as None
    # by default so __post_init__ can derive it from the result data;
    # locate_edit also passes an explicit value when it has cross-paragraph
    # context that __post_init__ can't see.
    safety_category: str | None = None
    # Phase 4 / Step 4.3: True when ``status == "ambiguous"`` because a
    # cross-paragraph existingText matched more than one valid window in
    # the document (vs. the regular single-paragraph ambiguous case
    # where multiple individual paragraph candidates matched). Both
    # cases route to manual review; the distinction lets the
    # diagnostics rollup count the cross-paragraph subset separately so
    # the run summary can show how often the model emitted a repeated
    # multi-paragraph quote. Default False keeps the regular ambiguous
    # path and every non-ambiguous result unchanged, and old resume
    # payloads load cleanly.
    cross_paragraph_ambiguous: bool = False
    # Phase 5 / Step 5.1: True when the finding had a CORRECTED verdict
    # with a non-empty ``verification.correction`` but the
    # :func:`replacement_style.correction_looks_replaceable` sanity
    # check failed — meaning the locator fell back to the model's
    # original ``replacement_text`` for the applied edit instead of
    # using the verifier's correction verbatim. The verifier's
    # correction is still preserved on
    # ``Finding.verification.correction`` so the report renders it as
    # the verifier's explanation; only the *applied* edit text changes.
    # ``apply_edits.execute_edit_plan`` sums this across locator
    # results to roll up
    # ``DiagnosticsReport.verifier_correction_rejected_as_replacement_count``.
    # Default False keeps the regular path and every legacy resume
    # payload unchanged.
    correction_rejected_as_replacement: bool = False

    def __post_init__(self) -> None:
        if self.safety_category is None:
            self.safety_category = _classify_locator_safety(
                status=self.status,
                action_type=(self.action_type or "").upper(),
                locations=self.locations,
                replacement_text=self.replacement_text,
                cross_paragraph=False,
            )


def _is_whole_paragraph_match(location: EditLocation) -> bool:
    return (
        location.mapping.element_type == "paragraph"
        and location.match_start == 0
        and location.match_end == len(location.mapping.text)
    )


def _formatting_downgrade(
    *,
    location: EditLocation,
    action_type: str,
    base_category: str,
) -> str:
    """Apply formatting downgrades for richly-formatted paragraphs.

    A paragraph counts as "richly formatted" when it has 2+ runs with
    distinct character-format signatures. Run-level replacement of a
    sub-span across such runs collapses non-matching formatting into the
    first run and silently destroys inline emphasis, so we downgrade.

    - Whole-paragraph replacements/DELETEs on a richly formatted paragraph
      are demoted to MANUAL_REVIEW.
    - Partial-run replacements that span multiple distinct-format runs are
      demoted to AUTO_WITH_CAUTION.

    Phase 3 / Step 3.1: when the mapping carries a fine-grained
    ``run_format_map`` (per-run ``(start, end, signature)`` triples in
    stripped-text coordinates), the partial-replacement check looks
    only at the runs the span actually crosses — an EDIT that lands
    entirely inside one uniformly-formatted region of a richly-
    formatted paragraph no longer downgrades, because the inline
    emphasis elsewhere in the paragraph is preserved. Legacy mappings
    without a per-run map (resume-state payloads from before Step 3.1,
    or non-extractor-built mappings used by tests) fall back to the
    coarse paragraph-level check.
    """
    if action_type not in {"EDIT", "DELETE"}:
        return base_category
    mapping = location.mapping
    if mapping.element_type != "paragraph":
        return base_category
    distinct = getattr(mapping, "distinct_formatting_runs", 0) or 0
    if distinct < 2:
        return base_category

    if _is_whole_paragraph_match(location):
        return SAFETY_MANUAL_REVIEW

    # Phase 3 / Step 3.1: span-aware check. When the per-run map is
    # available, look only at the runs the replacement span actually
    # crosses. If the span is entirely inside one uniformly-formatted
    # region, no inline emphasis is destroyed and we keep
    # ``base_category``. Empty / missing maps fall through to the
    # coarse paragraph-level downgrade so resume-state payloads from
    # before Step 3.1 stay conservative.
    run_format_map = getattr(mapping, "run_format_map", None)
    if run_format_map:
        runs_in_span = [
            (start, end, signature)
            for start, end, signature in run_format_map
            if start < location.match_end and end > location.match_start
        ]
        if runs_in_span:
            distinct_in_span = len({signature for _, _, signature in runs_in_span})
            if distinct_in_span < 2:
                return base_category

    # Partial replacement on a multi-format paragraph — caller must review.
    if base_category == SAFETY_AUTO_SAFE:
        return SAFETY_AUTO_WITH_CAUTION
    return base_category


def _classify_locator_safety(
    *,
    status: str,
    action_type: str,
    locations: list[EditLocation],
    replacement_text: str | None,
    cross_paragraph: bool,
) -> str:
    """Classify a locator result for downstream auto-apply gating."""
    if status == "not_found" or not locations:
        return SAFETY_REPORT_ONLY
    if status == "ambiguous":
        return SAFETY_MANUAL_REVIEW
    if action_type in {"EDIT", "ADD"} and not (replacement_text or "").strip():
        return SAFETY_REPORT_ONLY

    best = max(locations, key=lambda location: location.match_confidence)
    element_type = best.mapping.element_type
    method = best.match_method
    confidence = best.match_confidence

    if element_type in {"header", "footer", "meta"}:
        return SAFETY_MANUAL_REVIEW
    if cross_paragraph:
        category = SAFETY_AUTO_WITH_CAUTION
    elif method == "fuzzy":
        category = SAFETY_MANUAL_REVIEW
    elif method == "section_anchored_fuzzy":
        # Section-anchored matches whose underlying matcher was fuzzy are
        # still fuzzy text rediscovery — narrowing the search window does
        # not make a paraphrase identification safe enough for silent
        # document mutation. Route to manual review only.
        category = SAFETY_MANUAL_REVIEW
    elif method == "id":
        # Id-based match plus exact-text precondition is the strictest
        # signal the locator can produce — equivalent to an exact text
        # match but immune to whole-document duplicates. Body paragraphs
        # go AUTO_SAFE; table cells stay AUTO_WITH_CAUTION so the
        # table-cell precondition revalidation in spec_editor still gates
        # the actual mutation.
        category = SAFETY_AUTO_SAFE if element_type == "paragraph" else SAFETY_AUTO_WITH_CAUTION
    elif method == "exact" and confidence >= 0.95:
        category = SAFETY_AUTO_SAFE if element_type == "paragraph" else SAFETY_AUTO_WITH_CAUTION
    elif method == "normalized" and confidence >= 0.85:
        category = SAFETY_AUTO_SAFE if element_type == "paragraph" else SAFETY_AUTO_WITH_CAUTION
    elif method == "section_anchored":
        category = SAFETY_AUTO_WITH_CAUTION
    else:
        category = SAFETY_AUTO_WITH_CAUTION

    return _formatting_downgrade(
        location=best,
        action_type=action_type,
        base_category=category,
    )


def _resolve_replacement_text(finding: Finding) -> tuple[str | None, bool]:
    """Resolve the applied edit's replacement text from finding + verification.

    Returns ``(replacement_text, correction_rejected_as_replacement)``.

    The boolean is True only when the finding had a CORRECTED verdict
    with a non-empty ``verification.correction`` but
    :func:`replacement_style.correction_looks_replaceable` rejected the
    correction — in that case the verifier's correction is preserved
    on the result for the report, but the *applied* edit uses the
    model's original ``replacement_text``. The locator stamps the flag
    on every :class:`LocatorResult` it constructs so
    ``apply_edits.execute_edit_plan`` can sum it into the per-spec /
    run-level diagnostics counters.

    The legacy verbatim path is preserved behind
    ``SPEC_CRITIC_USE_VERIFIER_CORRECTION_AS_REPLACEMENT=1`` — when set,
    the sanity check is skipped and the correction is always used.
    """
    # Pull replacement text off the edit proposal (when present) so a
    # REPORT_ONLY finding cannot accidentally surface a stale quote left
    # in ``finding.replacementText`` by an earlier code path.
    proposal = finding.as_edit_proposal()
    base_replacement = proposal.replacement_text if proposal is not None else None
    verification = finding.verification
    if verification is None:
        return base_replacement, False
    if verification.verdict == "CORRECTED" and verification.correction:
        # Phase 5 / Step 5.1: sanity-check the verifier's correction
        # before treating it as clean replacement text. The verifier
        # prompt is optimized for explanation, not for substitution
        # into a CSI spec paragraph — corrections often carry
        # parenthetical citations, URLs, or temporal qualifiers that
        # don't belong in body text. When the check fails, fall back
        # to ``base_replacement`` (the model's own attempt at clean
        # replacement text) and flag the rejection so the diagnostics
        # rollup can surface "you may want to revisit these manually".
        if use_verifier_correction_as_replacement_enabled():
            return verification.correction, False
        if correction_looks_replaceable(verification.correction, base_replacement):
            return verification.correction, False
        return base_replacement, True
    if verification.verdict in ("CONFIRMED", "UNVERIFIED"):
        return base_replacement, False
    if verification.verdict == "DISPUTED":
        return None, False
    return base_replacement, False


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
    """Fuzzy match against the paragraph map.

    SequenceMatcher.ratio() over every paragraph is the dominant cost on
    long documents. We pre-filter with cheap length and quick_ratio gates
    before paying for the full ratio:

    * Length ratio: SequenceMatcher's max possible ratio is bounded by
      ``2 * min(len(a), len(b)) / (len(a) + len(b))``. If that ceiling is
      already below ``threshold``, ratio() cannot exceed it.
    * ``quick_ratio()`` is an upper bound that runs in O(n) on character
      bag intersections; if it is below threshold, ratio() will not pass.

    Both gates are conservative — they never reject a true positive — but
    typically eliminate 80–95% of paragraphs without any heavy work.
    """
    if not existing_text:
        return []
    hits: list[EditLocation] = []
    target_len = len(existing_text)
    for mapping in paragraph_map:
        m_len = len(mapping.text)
        if m_len == 0:
            continue
        # Length-ratio ceiling for SequenceMatcher.ratio.
        if 2.0 * min(target_len, m_len) / (target_len + m_len) < threshold:
            continue
        sm = SequenceMatcher(None, existing_text, mapping.text)
        if sm.quick_ratio() < threshold:
            continue
        ratio = sm.ratio()
        if ratio >= threshold:
            hits.append(
                EditLocation(
                    mapping=mapping,
                    match_start=0,
                    match_end=m_len,
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

    # Track which underlying matcher produced the hit so the classifier
    # can route section-anchored fuzzy matches to manual review only.
    # Relabeling every matcher's output as "section_anchored" would let a
    # fuzzy-derived match slip into AUTO_WITH_CAUTION and auto-apply.
    matchers: list[tuple[str, callable]] = [
        ("exact", lambda: _exact_match(existing_text, neighborhood, short_text=short_text)),
        ("normalized", lambda: _normalized_match(existing_text, neighborhood, short_text=short_text)),
        ("fuzzy", lambda: _fuzzy_match(existing_text, neighborhood)),
    ]
    for underlying, matcher in matchers:
        matches = matcher()
        if matches:
            method = "section_anchored_fuzzy" if underlying == "fuzzy" else "section_anchored"
            for location in matches:
                location.match_confidence = 0.70 if short_text else max(0.70, location.match_confidence)
                location.match_method = method
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


def _id_anchored_match(
    finding: Finding,
    existing_text: str,
    paragraph_map: list[ParagraphMapping],
) -> tuple[list[EditLocation], str | None]:
    """Locate the edit target by ``evidenceElementId`` with text revalidation.

    When the model emitted an element id, we trust it as the primary
    locator signal but still revalidate the recorded exact-text quote
    against the live element. The validation guarantees the id points at
    the same text the model saw at review time — if a later edit shifted
    the paragraph, the precondition will fail at apply time and the
    id-based ``LocatorResult`` will be regenerated from the live map on
    the next pass.

    Returns ``(locations, warning)`` where ``warning`` is non-empty only
    when the id was set but the locator could not turn it into a usable
    match — the caller treats that as a manual-review signal rather than
    silently falling back to fuzzy matching against the whole document
    (which would defeat the point of asking the model for an id).
    """
    evidence_id = (getattr(finding, "evidenceElementId", None) or "").strip()
    if not evidence_id:
        return [], None

    mapping = next(
        (m for m in paragraph_map if (m.element_id or "") == evidence_id),
        None,
    )
    if mapping is None:
        return [], (
            f"Finding cited evidenceElementId={evidence_id!r} but no element "
            "with that id exists in the extracted paragraph map. Manual "
            "review required."
        )

    # ADD without existingText: the id alone names the anchor paragraph.
    # We use the full text span so downstream ``_apply_add_action`` can
    # treat the whole paragraph as the anchor.
    if not existing_text:
        location = EditLocation(
            mapping=mapping,
            match_start=0,
            match_end=len(mapping.text),
            matched_text=mapping.text,
            match_confidence=1.0,
            match_method="id",
        )
        return [location], None

    # EDIT/DELETE (and ADD with anchorText): the model cited a specific
    # element AND a specific quote. Validate the quote inside that
    # element. We try exact substring first, then a normalized match so
    # whitespace/case differences in the quote don't break the id path.
    idx = mapping.text.find(existing_text)
    if idx != -1:
        location = EditLocation(
            mapping=mapping,
            match_start=idx,
            match_end=idx + len(existing_text),
            matched_text=mapping.text[idx:idx + len(existing_text)],
            match_confidence=1.0,
            match_method="id",
        )
        return [location], None

    norm_needle = _normalize_text(existing_text)
    if norm_needle:
        norm_text, index_map = _normalize_with_index_map(mapping.text)
        n_start = norm_text.find(norm_needle)
        if n_start != -1 and index_map:
            n_end = n_start + len(norm_needle) - 1
            if n_end < len(index_map):
                start = index_map[n_start]
                end = index_map[n_end] + 1
                location = EditLocation(
                    mapping=mapping,
                    match_start=start,
                    match_end=end,
                    matched_text=mapping.text[start:end],
                    match_confidence=0.95,
                    match_method="id",
                )
                return [location], None

    # The id is real but the quote no longer matches. Don't silently fall
    # back — that defeats the entire purpose of asking the model to cite
    # an id (the audit's "stop depending on fuzzy text rediscovery"). The
    # caller demotes the result to manual review.
    return [], (
        f"Finding cited evidenceElementId={evidence_id!r} but the "
        "existingText quote was not found inside that element. Manual "
        "review required to avoid wrong-span edits."
    )


def locate_edit(
    finding: Finding,
    paragraph_map: list[ParagraphMapping],
    *,
    min_confidence: float = 0.60,
) -> LocatorResult:
    # Findings without an edit proposal short-circuit here. REPORT_ONLY
    # and other non-edit findings get a clear ``status="not_found"`` /
    # ``safety_category=REPORT_ONLY`` result instead of falling through
    # the locator and producing fuzzy not-found warnings.
    proposal = finding.as_edit_proposal()
    if proposal is None:
        return LocatorResult(
            finding=finding,
            status="not_found",
            locations=[],
            replacement_text=None,
            action_type=(finding.actionType or "").upper(),
            warning=(
                "Finding has no edit proposal (REPORT_ONLY); locator returns "
                "no target. The finding still appears in the report."
            ),
            safety_category=SAFETY_REPORT_ONLY,
        )

    replacement, correction_rejected = _resolve_replacement_text(finding)
    action_type = proposal.action_type.upper()
    existing_text = (proposal.existing_text or "").strip()

    # ADD actions may rely on an explicit anchorText. If provided, locate
    # the anchor paragraph using the same matchers as EDIT.
    if action_type == "ADD":
        anchor_candidate = (proposal.anchor_text or "").strip()
        if anchor_candidate:
            existing_text = anchor_candidate

    # Prefer the element id when the model supplied one. The id path
    # validates the exact-text quote against the live element; if the
    # quote no longer matches, we return early with a manual-review
    # warning instead of falling through to fuzzy text matching. ID +
    # exact-text revalidation makes wrong-span replacements impossible
    # on the id path.
    id_locations, id_warning = _id_anchored_match(
        finding, existing_text, paragraph_map,
    )
    if id_locations:
        return LocatorResult(
            finding=finding,
            status="matched",
            locations=id_locations,
            replacement_text=replacement,
            action_type=action_type,
            warning=None,
            # The id path is strictly safer than text matching: the model
            # asserted "this element" and the exact-text quote still
            # holds inside that element. Whole-paragraph id matches on a
            # body paragraph qualify as AUTO_SAFE, but the existing
            # formatting downgrades still apply via the standard
            # classifier (multi-format paragraph → MANUAL_REVIEW), so
            # we route through ``_classify_locator_safety`` for
            # consistency rather than hard-coding AUTO_SAFE here.
            safety_category=_classify_locator_safety(
                status="matched",
                action_type=action_type,
                locations=id_locations,
                replacement_text=replacement,
                cross_paragraph=False,
            ),
            correction_rejected_as_replacement=correction_rejected,
        )
    if id_warning:
        # Id was set but unusable — manual review only. Do not fall back
        # to text matching: the model named a specific element, so a
        # different paragraph that happens to contain similar text is
        # almost certainly the wrong target.
        return LocatorResult(
            finding=finding,
            status="not_found",
            locations=[],
            replacement_text=replacement,
            action_type=action_type,
            warning=id_warning,
            safety_category=SAFETY_MANUAL_REVIEW,
            correction_rejected_as_replacement=correction_rejected,
        )

    if not existing_text:
        return LocatorResult(
            finding=finding,
            status="not_found",
            locations=[],
            replacement_text=replacement,
            action_type=action_type,
            warning="Finding has no existingText; locator cannot determine an edit target.",
            safety_category=SAFETY_REPORT_ONLY,
            correction_rejected_as_replacement=correction_rejected,
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
            best_span = max(filtered_spans, key=lambda span: span[0].match_confidence)
            multi_window = len(filtered_spans) > 1
            cross_status = "matched" if not multi_window else "ambiguous"
            # Phase 4 / Step 4.3: distinguish the multi-window
            # cross-paragraph ambiguous case from the single-window
            # cross-paragraph matched case in the warning text. All
            # cross-paragraph windows carry the same flat 0.88
            # confidence, so picking ``best_span`` here would be
            # equivalent to insertion-order if we silently applied it.
            # Explicit safety_category=SAFETY_MANUAL_REVIEW + a clear
            # warning makes the manual-review requirement visible to
            # the user.
            if multi_window:
                warning = (
                    "Cross-paragraph existingText matched multiple "
                    f"identical {len(filtered_spans)}-paragraph windows in "
                    "the document; manual review required to disambiguate."
                )
                safety_category = SAFETY_MANUAL_REVIEW
            else:
                warning = (
                    "Matched text spans multiple paragraphs; review "
                    "before auto-applying edit."
                )
                safety_category = _classify_locator_safety(
                    status=cross_status,
                    action_type=action_type,
                    locations=best_span,
                    replacement_text=replacement,
                    cross_paragraph=True,
                )
            return LocatorResult(
                finding=finding,
                status=cross_status,
                locations=best_span,
                replacement_text=replacement,
                action_type=action_type,
                warning=warning,
                safety_category=safety_category,
                cross_paragraph_ambiguous=multi_window,
                correction_rejected_as_replacement=correction_rejected,
            )
        return LocatorResult(
            finding=finding,
            status="not_found",
            locations=[],
            replacement_text=replacement,
            action_type=action_type,
            warning="No paragraph match met the confidence threshold.",
            safety_category=SAFETY_REPORT_ONLY,
            correction_rejected_as_replacement=correction_rejected,
        )

    status = "matched" if len(match_candidates) == 1 else "ambiguous"
    return LocatorResult(
        finding=finding,
        status=status,
        locations=match_candidates,
        replacement_text=replacement,
        action_type=action_type,
        warning=warning,
        safety_category=_classify_locator_safety(
            status=status,
            action_type=action_type,
            locations=match_candidates,
            replacement_text=replacement,
            cross_paragraph=False,
        ),
        correction_rejected_as_replacement=correction_rejected,
    )


def locate_edits(
    findings: list[Finding],
    paragraph_map: list[ParagraphMapping],
    *,
    min_confidence: float = 0.60,
) -> list[LocatorResult]:
    return [locate_edit(finding, paragraph_map, min_confidence=min_confidence) for finding in findings]


def locator_evidence_from_result(result: LocatorResult) -> dict:
    """Extract the locator-evidence snapshot from a :class:`LocatorResult`.

    Chunk 4 / Trust Upgrade helper. The dict is the wire format stashed
    onto :attr:`Finding.locator_evidence` so the report exporter can
    render the "Edit Target Evidence" panel (match method, confidence,
    safety category, element id) without re-running the locator. The
    shape is JSON-safe so it round-trips through resume state cleanly.

    The "best" location is the one with the highest match confidence —
    same selection rule used throughout the locator and edit pipeline.
    When the locator returned no usable location (``not_found`` /
    ``ambiguous``), the match-method / confidence / element-id fields
    are empty so the report can render "Not located" without inventing
    a confidence value.
    """
    locations = list(result.locations or [])
    if locations:
        best = max(locations, key=lambda loc: loc.match_confidence)
        match_method = str(best.match_method or "")
        match_confidence = float(best.match_confidence or 0.0)
        element_id = str(getattr(best.mapping, "element_id", "") or "")
    else:
        match_method = ""
        match_confidence = 0.0
        element_id = ""
    return {
        "status": str(result.status or ""),
        "match_method": match_method,
        "match_confidence": match_confidence,
        "safety_category": str(result.safety_category or ""),
        "element_id": element_id,
    }
