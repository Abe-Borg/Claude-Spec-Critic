"""
Preprocessor module for local detection of LEED references and placeholders.

This module performs DETECTION ONLY — it does not modify spec content.
Detected items are reported separately from LLM findings to:
    1. Save tokens (no need to ask Claude to find [INSERT] placeholders)
    2. Provide instant feedback (no API call required)
    3. Keep concerns separate (editorial markers vs. technical issues)

If you need actual document cleanup/scrubbing (removing boilerplate, fixing
formatting, etc.), use the separate SpecCleanse tool:
https://github.com/Abe-Borg/Spec_Cleanse

Detection categories:
    - LEED references: LEED, LEED-NC, LEED-CI, USGBC
      (K-12 DSA projects typically aren't LEED — these are likely copy/paste errors)
    - Placeholders: [INSERT...], [VERIFY...], [TBD], ___, etc.
      (Unresolved editorial markers that need attention before issuing)

Usage:
    from preprocessor import preprocess_spec, PreprocessResult
    
    result = preprocess_spec(spec_content, "23 21 13 - Hydronic Piping.docx")
    print(f"Found {len(result.leed_alerts)} LEED references")
    print(f"Found {len(result.placeholder_alerts)} placeholders")
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Optional

from ..core.code_cycles import CodeCycle
from ..modules import DetectorVocabulary, module_for_cycle


@dataclass
class PreprocessResult:
    """
    Result of detection-only preprocessing for a single spec.

    Attributes:
        leed_alerts: List of detected LEED references with context
        placeholder_alerts: List of detected placeholders with context
        code_cycle_alerts: References to a stale California code cycle
            (e.g. ``2019 CBC`` when the selected cycle is 2025).
        structural_alerts: Empty sections and duplicate headings detected
            without spending model tokens.
        template_marker_alerts: Additional editorial markers (``TODO``,
            ``FIXME``, ``XXX``, ``???``, lorem-ipsum boilerplate) that the
            existing ``placeholder_alerts`` regexes do not match.
        invalid_code_cycle_alerts: California code citations whose year is
            not a real cycle (e.g. ``2018 CBC`` or ``2020 CMC``). The
            ``code_cycle_alerts`` list only catches *stale* but otherwise
            plausible years; an invalid year is a clear typo or
            fabrication.
        duplicate_paragraph_alerts: Verbatim duplicate paragraphs of
            substantial length (≥80 chars by default). A clear
            deterministic signal for copy-paste mistakes that does not
            require LLM tokens.

    Each alert is a dict with keys:
        - filename: Source file name
        - type: Description of what was matched (e.g., "LEED reference")
        - match: The actual matched text
        - context: ~120 char window around the match for human review
        - position: Character offset in the document
        - deterministic_rule: Stable rule id (``leed_reference``,
          ``placeholder``, ``stale_code_cycle``, ``stale_asce7``,
          ``empty_section``, ``duplicate_heading``, ``template_marker``,
          ``invalid_code_cycle``, ``duplicate_paragraph``,
          ``inconsistent_filename``) so reports / verification routing /
          diagnostics can branch on the rule without keyword-sniffing the
          human-readable ``type`` string.
    """
    leed_alerts: list[dict] = field(default_factory=list)
    placeholder_alerts: list[dict] = field(default_factory=list)
    code_cycle_alerts: list[dict] = field(default_factory=list)
    structural_alerts: list[dict] = field(default_factory=list)
    template_marker_alerts: list[dict] = field(default_factory=list)
    invalid_code_cycle_alerts: list[dict] = field(default_factory=list)
    duplicate_paragraph_alerts: list[dict] = field(default_factory=list)


# Stable rule identifiers so every consumer (report, verification router,
# diagnostics) can branch on a known string instead of sniffing the
# human-readable ``type`` field. Defined at module level so tests and
# downstream modules can import the canonical names.
DETERMINISTIC_RULE_LEED: str = "leed_reference"
DETERMINISTIC_RULE_PLACEHOLDER: str = "placeholder"
DETERMINISTIC_RULE_STALE_CODE_CYCLE: str = "stale_code_cycle"
DETERMINISTIC_RULE_STALE_ASCE7: str = "stale_asce7"
DETERMINISTIC_RULE_EMPTY_SECTION: str = "empty_section"
DETERMINISTIC_RULE_DUPLICATE_HEADING: str = "duplicate_heading"
DETERMINISTIC_RULE_TEMPLATE_MARKER: str = "template_marker"
DETERMINISTIC_RULE_INVALID_CODE_CYCLE: str = "invalid_code_cycle"
DETERMINISTIC_RULE_DUPLICATE_PARAGRAPH: str = "duplicate_paragraph"
DETERMINISTIC_RULE_INCONSISTENT_FILENAME: str = "inconsistent_filename"


# -----------------------------------------------------------------------------
# Detection Patterns
# -----------------------------------------------------------------------------

LEED_PATTERNS: list[tuple[str, str]] = [
    # Specific patterns first so they claim spans before the generic \bLEED\b
    (r"(?i)\bLEED[-\s]?NC\b", "LEED-NC reference"),
    (r"(?i)\bLEED[-\s]?CI\b", "LEED-CI reference"),
    (r"(?i)\bLEED[-\s]?EB\b", "LEED-EB reference"),
    (r"(?i)\bUSGBC\b", "USGBC reference"),
    (r"(?i)\bLEED\b", "LEED reference"),  # Generic last
]

PLACEHOLDER_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)\[\s*INSERT[^\]]*\]", "INSERT placeholder"),
    (r"(?i)\[\s*VERIFY[^\]]*\]", "VERIFY placeholder"),
    (r"(?i)\[\s*EDIT[^\]]*\]", "EDIT placeholder"),
    (r"(?i)\[\s*SELECT[^\]]*\]", "SELECT placeholder"),
    (r"(?i)\[\s*COORDINATE[^\]]*\]", "COORDINATE placeholder"),
    (r"(?i)\[\s*TO\s+BE\s+DETERMINED[^\]]*\]", "TBD placeholder"),
    (r"(?i)\[\s*TBD[^\]]*\]", "TBD placeholder"),
    (r"(?i)\[\s*N\/A[^\]]*\]", "N/A placeholder"),
    (r"(?i)\[\s*OPTION[^\]]*\]", "OPTION placeholder"),
    (r"(?i)<\s*VERIFY[^>]*>", "VERIFY tag"),
    (r"(?i)<\s*EDIT[^>]*>", "EDIT tag"),
    (r"(?i)<\s*INSERT[^>]*>", "INSERT tag"),
    (r"_{3,}", "Underscore placeholder"),
    (r"\[\s*\.\.\.\s*\]", "Ellipsis placeholder"),
]


# -----------------------------------------------------------------------------
# Detection Functions
# -----------------------------------------------------------------------------
def _find_matches(
    patterns: Iterable[tuple[str, str]],
    content: str,
    filename: str,
    max_matches: int,
    *,
    rule_id: str = "",
) -> list[dict]:
    """Find all matches for a set of regex patterns in content.

    Uses span-based deduplication: if a match's character range is fully
    contained within an already-recorded span, it is skipped. This prevents
    e.g. "LEED-NC" from producing both a "LEED-NC reference" alert and a
    duplicate "LEED reference" alert for the "LEED" substring.

    Every alert is stamped with ``deterministic_rule = rule_id`` so
    downstream consumers can branch on the rule without keyword-sniffing
    the human-readable ``type`` string.
    """
    alerts: list[dict] = []
    seen_spans: list[tuple[int, int]] = []
    for pattern, description in patterns:
        try:
            for match in re.finditer(pattern, content):
                m_start, m_end = match.start(), match.end()
                # Skip if this span overlaps with an already-seen span
                if any(s <= m_start and m_end <= e for s, e in seen_spans):
                    continue
                seen_spans.append((m_start, m_end))

                ctx_start = max(0, m_start - 60)
                ctx_end = min(len(content), m_end + 60)
                ctx = content[ctx_start:ctx_end].replace("\n", " ").strip()

                alerts.append(
                    {
                        "filename": filename,
                        "type": description,
                        "match": match.group(0),
                        "context": ctx,
                        "position": m_start,
                        "deterministic_rule": rule_id,
                    }
                )

                if len(alerts) >= max_matches:
                    return alerts
        except re.error:
            continue
    return alerts


def detect_leed_references(content: str, filename: str, max_matches: int = 50) -> list[dict]:
    """Detect LEED-related references in spec content."""
    return _find_matches(
        LEED_PATTERNS,
        content,
        filename,
        max_matches=max_matches,
        rule_id=DETERMINISTIC_RULE_LEED,
    )


def detect_placeholders(content: str, filename: str, max_matches: int = 200) -> list[dict]:
    """Detect unresolved placeholders and editorial markers in spec content."""
    return _find_matches(
        PLACEHOLDER_PATTERNS,
        content,
        filename,
        max_matches=max_matches,
        rule_id=DETERMINISTIC_RULE_PLACEHOLDER,
    )


# -----------------------------------------------------------------------------
# Additional local preflight checks.
#
# These run before any model call and surface deterministic issues that should
# never need an LLM round-trip. Keeping them here means a re-run with toggled
# project options does not pay tokens for catching a stale ``2019 CBC``
# reference or an empty section heading.
# -----------------------------------------------------------------------------

# The year/code vocabulary (abbreviations, plausible/valid year sets, extra
# long-form patterns, the LEED appropriateness flag) is module data —
# ``DetectorVocabulary`` on the owning ``ReviewModule``, resolved through the
# registry's unique-label bridge. The detector LOGIC below (regex assembly,
# span dedup, the negation-suppression window, sentence narrowing) stays
# engine-owned so a module cannot change detection semantics, only the
# domain facts scanned for.


def _default_vocabulary() -> DetectorVocabulary:
    """Vocabulary used when a caller has no cycle (degrades to the default module)."""
    return module_for_cycle(None).detector_vocabulary


@lru_cache(maxsize=8)
def _stale_cycle_patterns_for(vocabulary: DetectorVocabulary) -> tuple[re.Pattern, ...]:
    """Compile the year/code citation patterns for one module's vocabulary.

    Two engine patterns (``"<year> <code>"`` and ``"<code> <year>"``) built
    from the vocabulary's abbreviations, plus any module-supplied long-form
    patterns (each captures the year as group 1 — validated at module
    registration). Cached per vocabulary: ``DetectorVocabulary`` is frozen
    and tuple-typed, so it is hashable, and the registry holds a handful of
    modules at most.
    """
    abbrev_alt = "|".join(re.escape(a) for a in vocabulary.code_abbreviations)
    patterns = [
        re.compile(r"\b(20\d{2})\s+(?:" + abbrev_alt + r")\b", flags=re.IGNORECASE),
        re.compile(r"\b(?:" + abbrev_alt + r")[\s,]+(20\d{2})\b", flags=re.IGNORECASE),
    ]
    patterns.extend(
        re.compile(source, flags=re.IGNORECASE)
        for source in vocabulary.stale_cycle_extra_patterns
    )
    return tuple(patterns)


# ASCE 7 edition references. Only flag editions older than the cycle's
# nominal ASCE 7 edition (e.g. 7-10 / 7-05 when cycle says 7-22). The
# pattern is structural (engine); the recognition whitelist of real,
# published editions lives on the module vocabulary
# (``asce7_plausible_editions``) so a stray capture like "ASCE 7-42" is
# ignored while every genuine edition older than the cycle's nominal one is
# still flagged (TRUST_AUDIT P2-1).
_ASCE7_PATTERN = re.compile(r"\bASCE[\s-]*7[\s-]*(\d{2})\b", flags=re.IGNORECASE)


def _asce7_edition_year(two_digit: str) -> int:
    """Widen a two-digit ASCE 7 edition to its full publication year.

    ASCE 7 editions span 1988–2022, so a naive two-digit comparison inverts
    across the century boundary: ``int("98") >= int("22")`` would treat the
    1998 edition as *newer* than 2022 and skip it (TRUST_AUDIT P2-1). Editions
    ``>= 80`` are 1900s, the rest 2000s — a safe pivot given ASCE 7 began at
    7-88 and no plausible future edition reaches 7-80.
    """
    yr = int(two_digit)
    return 1900 + yr if yr >= 80 else 2000 + yr


# Terms that, when they appear shortly *before* a stale-cycle match,
# signal the author is describing an old reference rather than requiring
# it. The window is intentionally small so a negation in a different
# sentence does not silently suppress an active requirement.
#
# Each pattern is a whole-word match (and ``no longer`` is matched as a
# two-word phrase). Bare ``not`` is intentionally NOT a suppressor; the
# matcher only treats it as one when it is genuinely a verb-phrase
# negation — see ``_should_suppress_stale_cycle``.
_STALE_CYCLE_SUPPRESS_WINDOW: int = 80

_STALE_CYCLE_SUPPRESS_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bpreviously\b", flags=re.IGNORECASE),
    re.compile(r"\bformerly\b", flags=re.IGNORECASE),
    re.compile(r"\bsuperseded\b", flags=re.IGNORECASE),
    re.compile(r"\bwithdrawn\b", flags=re.IGNORECASE),
    re.compile(r"\bobsolete\b", flags=re.IGNORECASE),
    # "no longer" only as a phrase — single-word ``no`` is too noisy.
    re.compile(r"\bno\s+longer\b", flags=re.IGNORECASE),
    # ``prior`` and ``historical`` are common enough in spec prose that we
    # only suppress when the keyword appears in the immediately preceding
    # window (the regex itself is whole-word).
    re.compile(r"\bprior\b", flags=re.IGNORECASE),
    re.compile(r"\bhistorical\b", flags=re.IGNORECASE),
    # ``shall not / will not / does not / is not`` plus a small set of
    # related contractions: the model author is explicitly negating the
    # requirement that follows. We deliberately do NOT match bare ``not``
    # because phrases like "Section X is also referenced in 2019 CBC and
    # not 2022 CBC" would otherwise suppress the wrong year.
    re.compile(r"\b(?:shall|will|does|do|is|are|was|were|must|may|can)\s+not\b", flags=re.IGNORECASE),
    re.compile(r"\b(?:isn't|wasn't|aren't|weren't|won't|don't|doesn't|shan't|mustn't|can't|cannot)\b", flags=re.IGNORECASE),
)


def _should_suppress_stale_cycle(
    content: str, match_start: int, match_end: int
) -> bool:
    """Return True when a stale-cycle match is qualified by a negation term.

    Searches up to ``_STALE_CYCLE_SUPPRESS_WINDOW`` characters on either
    side of the match for a whole-word negation / historical keyword
    (e.g. ``previously per 2019 CBC`` or ``2022 CBC is no longer used``).
    When found, treat the citation as descriptive (not an active
    requirement) and skip the alert. The window is capped so a negation
    in a different sentence does not bleed across; sentence-terminating
    punctuation (``.``, ``;``, ``\\n\\n``) inside the window narrows the
    effective scan to the matching sentence to keep false-suppressions
    rare in dense prose.
    """
    if not content:
        return False
    pre_start = max(0, match_start - _STALE_CYCLE_SUPPRESS_WINDOW)
    pre_window = content[pre_start:match_start]
    # Restrict the *preceding* window to the current sentence so a
    # negation in a previous clause doesn't suppress the active one.
    for term in (".", ";", "\n\n"):
        cut = pre_window.rfind(term)
        if cut >= 0:
            pre_window = pre_window[cut + len(term):]
    post_end = min(len(content), match_end + _STALE_CYCLE_SUPPRESS_WINDOW)
    post_window = content[match_end:post_end]
    # Same for the *trailing* window: stop at the next sentence boundary.
    for term in (".", ";", "\n\n"):
        cut = post_window.find(term)
        if cut >= 0:
            post_window = post_window[:cut]
            break
    candidates = (pre_window, post_window)
    if not any(w.strip() for w in candidates):
        return False
    return any(
        pat.search(w)
        for w in candidates
        if w
        for pat in _STALE_CYCLE_SUPPRESS_PATTERNS
    )


def detect_stale_code_cycle_references(
    content: str,
    filename: str,
    cycle: CodeCycle,
    *,
    max_matches: int = 200,
) -> list[dict]:
    """Flag year/edition references that do not match the selected cycle.

    A reference is "stale" when it pins a code year that is different from
    the cycle's ``primary_code_year`` (e.g. ``2019 CBC`` selected against
    the 2025 cycle), or when an ASCE 7 edition is older than ``cycle.asce7``.
    The abbreviation / year vocabulary comes from the owning module's
    :class:`DetectorVocabulary` (resolved via the unique-label bridge).

    The detector is intentionally narrow: it never flags the cycle's own year
    or its prior cycle's year if the prior cycle is being referenced as
    historical context (the model still has the project context to qualify
    that). Callers can downgrade alerts by post-processing the returned
    dicts; this function does not call the API.
    """
    if not cycle:
        return []
    vocabulary = module_for_cycle(cycle).detector_vocabulary
    target_year = (cycle.primary_code_year or "").strip()
    if not target_year:
        return []

    plausible_years = frozenset(vocabulary.plausible_cycle_years)
    alerts: list[dict] = []
    seen_spans: list[tuple[int, int]] = []
    for pattern in _stale_cycle_patterns_for(vocabulary):
        for match in pattern.finditer(content):
            year = next((g for g in match.groups() if g and g in plausible_years), None)
            if year is None or year == target_year:
                continue
            span = (match.start(), match.end())
            if any(s <= span[0] and span[1] <= e for s, e in seen_spans):
                continue
            # Skip citations preceded by a negation / historical keyword
            # in the immediate window. Recorded spans still get tracked
            # above so a suppressed match doesn't bleed into the overlap
            # dedup for downstream patterns.
            if _should_suppress_stale_cycle(content, span[0], span[1]):
                seen_spans.append(span)
                continue
            seen_spans.append(span)
            ctx_start = max(0, span[0] - 60)
            ctx_end = min(len(content), span[1] + 60)
            alerts.append(
                {
                    "filename": filename,
                    "type": f"Stale code cycle reference ({year} vs selected {target_year})",
                    "match": match.group(0),
                    "context": content[ctx_start:ctx_end].replace("\n", " ").strip(),
                    "position": span[0],
                    "expected_year": target_year,
                    "found_year": year,
                    "deterministic_rule": DETERMINISTIC_RULE_STALE_CODE_CYCLE,
                }
            )
            if len(alerts) >= max_matches:
                return alerts

    target_asce = re.sub(r"\D", "", cycle.asce7 or "")
    if target_asce and len(target_asce) >= 2:
        target_asce_yr = target_asce[-2:]
        target_asce_year = _asce7_edition_year(target_asce_yr)
        for match in _ASCE7_PATTERN.finditer(content):
            edition = match.group(1)
            # Century-aware comparison: a 1998 edition is older than 2022 even
            # though ``98 > 22`` numerically. Unknown two-digit captures (not a
            # real edition) are ignored to avoid flagging stray numbers.
            if (
                edition not in vocabulary.asce7_plausible_editions
                or _asce7_edition_year(edition) >= target_asce_year
            ):
                continue
            span = (match.start(), match.end())
            if any(s <= span[0] and span[1] <= e for s, e in seen_spans):
                continue
            # Same suppression for ASCE 7 — a sentence that explicitly
            # says "no longer use ASCE 7-10" is descriptive, not a
            # requirement.
            if _should_suppress_stale_cycle(content, span[0], span[1]):
                seen_spans.append(span)
                continue
            seen_spans.append(span)
            ctx_start = max(0, span[0] - 60)
            ctx_end = min(len(content), span[1] + 60)
            alerts.append(
                {
                    "filename": filename,
                    "type": f"Stale ASCE 7 edition (7-{edition} vs selected {cycle.asce7})",
                    "match": match.group(0),
                    "context": content[ctx_start:ctx_end].replace("\n", " ").strip(),
                    "position": span[0],
                    "expected_edition": cycle.asce7,
                    "found_edition": f"7-{edition}",
                    "deterministic_rule": DETERMINISTIC_RULE_STALE_ASCE7,
                }
            )
            if len(alerts) >= max_matches:
                return alerts
    return alerts


# Numbered CSI-style heading at the start of a paragraph: "1.01", "2.3 ",
# "PART 1", "1.0 GENERAL", etc. We anchor at the start of a paragraph
# (preceded by paragraph delimiter "\n\n" or string start).
_HEADING_LINE_RE = re.compile(
    r"(?:^|\n\n)\s*(?P<num>(?:PART\s+\d+|\d+(?:\.\d+){0,2}))\s+(?P<title>[^\n]{1,120})",
    flags=re.IGNORECASE,
)


def _iter_section_headings(content: str):
    """Yield ``(number, title, start, end)`` tuples for spec section headings."""
    for match in _HEADING_LINE_RE.finditer(content):
        number = match.group("num").strip().upper()
        title = match.group("title").strip().rstrip(":").strip()
        if not title:
            continue
        yield number, title, match.start("num"), match.end("title")


def detect_empty_sections(
    content: str,
    filename: str,
    *,
    max_matches: int = 50,
) -> list[dict]:
    """Flag numbered headings whose body content is empty or whitespace.

    "Empty" means: the heading is followed by another heading (or end of
    document) with no body paragraph between them. This catches templated
    DSA specs where an editor deleted the body without removing the
    heading scaffold.
    """
    headings = list(_iter_section_headings(content))
    if not headings:
        return []
    alerts: list[dict] = []
    for i, (number, title, h_start, h_end) in enumerate(headings):
        body_end = headings[i + 1][2] if i + 1 < len(headings) else len(content)
        body = content[h_end:body_end].strip()
        if body:
            continue
        ctx_start = max(0, h_start - 40)
        ctx_end = min(len(content), body_end + 40)
        alerts.append(
            {
                "filename": filename,
                "type": "Empty section",
                "match": f"{number} {title}",
                "context": content[ctx_start:ctx_end].replace("\n", " ").strip(),
                "position": h_start,
                "section_number": number,
                "section_title": title,
                "deterministic_rule": DETERMINISTIC_RULE_EMPTY_SECTION,
            }
        )
        if len(alerts) >= max_matches:
            break
    return alerts


def detect_duplicate_headings(
    content: str,
    filename: str,
    *,
    max_matches: int = 50,
) -> list[dict]:
    """Flag the same section number appearing more than once.

    DSA specs occasionally end up with a second copy of section ``2.01`` after
    a copy/paste edit. The reviewer can still flag it, but catching it
    locally avoids paying tokens for a deterministic structural mistake.
    """
    counts: dict[str, list[tuple[str, int]]] = {}
    for number, title, h_start, _ in _iter_section_headings(content):
        counts.setdefault(number, []).append((title, h_start))

    alerts: list[dict] = []
    for number, occurrences in counts.items():
        if len(occurrences) < 2:
            continue
        # Report each occurrence after the first so users see every duplicate.
        for title, h_start in occurrences[1:]:
            ctx_start = max(0, h_start - 60)
            ctx_end = min(len(content), h_start + 120)
            alerts.append(
                {
                    "filename": filename,
                    "type": "Duplicate section heading",
                    "match": f"{number} {title}",
                    "context": content[ctx_start:ctx_end].replace("\n", " ").strip(),
                    "position": h_start,
                    "section_number": number,
                    "occurrence_count": len(occurrences),
                    "deterministic_rule": DETERMINISTIC_RULE_DUPLICATE_HEADING,
                }
            )
            if len(alerts) >= max_matches:
                return alerts
    return alerts


# CSI-style filenames: "23 21 13 - Hydronic Piping.docx" etc. We accept either
# space-separated triples or hyphen-separated triples but flag mixed styles
# within a single project.
_CSI_FILENAME_RE = re.compile(
    r"^\s*(\d{2})\s*(?P<sep>[\s-])\s*(\d{2})\s*(?P=sep)\s*(\d{2})\b"
)


def detect_inconsistent_file_naming(filenames: list[str]) -> list[dict]:
    """Project-level (cross-file) check for mixed CSI naming conventions.

    Returns one alert per non-conforming file when the project uses a
    dominant naming style. Used by the GUI/pipeline to warn before
    submission. No model tokens are spent.
    """
    if len(filenames) < 2:
        return []
    sep_counts: dict[str, int] = {"space": 0, "dash": 0, "other": 0}
    parsed: dict[str, str] = {}
    for fname in filenames:
        match = _CSI_FILENAME_RE.match(fname)
        if not match:
            parsed[fname] = "other"
            sep_counts["other"] += 1
            continue
        sep = match.group("sep")
        style = "space" if sep == " " else "dash"
        parsed[fname] = style
        sep_counts[style] += 1

    dominant = max(sep_counts, key=lambda k: sep_counts[k])
    if sep_counts[dominant] == 0 or dominant == "other":
        return []
    alerts: list[dict] = []
    for fname, style in parsed.items():
        if style == dominant:
            continue
        alerts.append(
            {
                "filename": fname,
                "type": f"Inconsistent CSI filename style (expected {dominant}-separated)",
                "match": fname,
                "context": fname,
                "position": 0,
                "dominant_style": dominant,
                "found_style": style,
                "deterministic_rule": DETERMINISTIC_RULE_INCONSISTENT_FILENAME,
            }
        )
    return alerts


# -----------------------------------------------------------------------------
# Additional deterministic checks.
#
# These rules expand the local preflight surface so simple, repetitive,
# high-confidence issues can be found without paying LLM tokens. Each rule:
#   - Produces the same alert-dict shape as the existing detectors.
#   - Stamps ``deterministic_rule`` with a stable id (see DETERMINISTIC_RULE_*).
#   - Documents its intentional scope so the detector does not overreach
#     into code interpretation.
# -----------------------------------------------------------------------------

# Template markers that the existing PLACEHOLDER_PATTERNS does *not* catch.
# Each rule below has been chosen to minimize false positives:
#   - TODO / FIXME / XXX / HACK / NOTE — require a delimiter ("\bTODO:" or
#     "TODO followed by an uppercase word" so phrases like "to do list"
#     don't trigger).
#   - "???"  — three or more consecutive question marks; valid prose almost
#     never has this.
#   - "Lorem ipsum" — fragment of the canonical lorem-ipsum boilerplate
#     occasionally left in template starter specs.
_TEMPLATE_MARKER_PATTERNS: list[tuple[str, str]] = [
    (r"\bTODO\s*:", "TODO marker"),
    (r"\bTODO\b(?=\s+[A-Z])", "TODO marker"),
    (r"\bFIXME\b", "FIXME marker"),
    (r"\bXXX\b(?!\d|-)", "XXX marker"),
    (r"\bHACK\b\s*:", "HACK marker"),
    (r"\?{3,}", "Question-mark placeholder"),
    (r"(?i)\bLorem ipsum\b", "Lorem ipsum boilerplate"),
]


def detect_unresolved_template_markers(
    content: str,
    filename: str,
    *,
    max_matches: int = 200,
) -> list[dict]:
    """Flag editorial / template markers missed by ``detect_placeholders``.

    Catches ``TODO:``, ``FIXME``, ``XXX``, ``???`` and lorem-ipsum text.
    The regexes are intentionally conservative — see _TEMPLATE_MARKER_PATTERNS
    for the per-rule rationale — so that prose like "to do list" or model
    numbers containing "XXX-12" never trigger a false positive.
    """
    return _find_matches(
        _TEMPLATE_MARKER_PATTERNS,
        content,
        filename,
        max_matches=max_matches,
        rule_id=DETERMINISTIC_RULE_TEMPLATE_MARKER,
    )


def detect_invalid_code_cycle_strings(
    content: str,
    filename: str,
    *,
    vocabulary: DetectorVocabulary | None = None,
    max_matches: int = 100,
) -> list[dict]:
    """Flag year/code citations whose year is not a real published cycle.

    The vocabulary's ``valid_cycle_years`` lists every year the jurisdiction
    has published (or announced) a cycle for — for California: 2010, 2013,
    2016, 2019, 2022, 2025, and the anticipated 2028. A reference like
    ``2018 CBC`` or ``2024 CMC`` is a clear typo / fabrication that the LLM
    review does not need to discover — surface it locally. When
    ``vocabulary`` is omitted, the default module's vocabulary applies
    (``preprocess_spec`` passes the owning module's explicitly).

    The detector reuses the same year/code patterns as the stale-cycle
    detector but applies a *different* admissibility test:
        - Stale-cycle path : year is in ``plausible_cycle_years`` but not
          the selected cycle's primary year.
        - Invalid path     : year matches a real-looking ``20\\d{2}`` but is
          NOT in ``valid_cycle_years``.
    The two detectors do not collide because ``plausible_cycle_years`` is a
    subset of ``valid_cycle_years`` (enforced at module registration), so
    their admissibility sets are disjoint by construction.
    """
    vocab = vocabulary if vocabulary is not None else _default_vocabulary()
    valid_years = frozenset(vocab.valid_cycle_years)
    jurisdiction = vocab.jurisdiction_label.strip()
    type_prefix = (
        f"Invalid {jurisdiction} code cycle year" if jurisdiction
        else "Invalid code cycle year"
    )
    alerts: list[dict] = []
    seen_spans: list[tuple[int, int]] = []
    for pattern in _stale_cycle_patterns_for(vocab):
        for match in pattern.finditer(content):
            year = next((g for g in match.groups() if g and re.fullmatch(r"20\d{2}", g)), None)
            if year is None or year in valid_years:
                continue
            span = (match.start(), match.end())
            if any(s <= span[0] and span[1] <= e for s, e in seen_spans):
                continue
            seen_spans.append(span)
            ctx_start = max(0, span[0] - 60)
            ctx_end = min(len(content), span[1] + 60)
            alerts.append(
                {
                    "filename": filename,
                    "type": f"{type_prefix} ({year})",
                    "match": match.group(0),
                    "context": content[ctx_start:ctx_end].replace("\n", " ").strip(),
                    "position": span[0],
                    "found_year": year,
                    "deterministic_rule": DETERMINISTIC_RULE_INVALID_CODE_CYCLE,
                }
            )
            if len(alerts) >= max_matches:
                return alerts
    return alerts


# Minimum length (in characters) for a paragraph to be considered for the
# duplicate-paragraph detector. Short paragraphs ("PART 1", "SECTION 23 21 13",
# numbered subheadings, etc.) repeat by design and would generate noise. 80
# characters is roughly one short sentence — large enough that an exact
# duplicate is meaningful, small enough to catch a single repeated bullet.
_DUPLICATE_PARAGRAPH_MIN_LENGTH: int = 80


def detect_duplicate_paragraphs(
    content: str,
    filename: str,
    *,
    min_length: int = _DUPLICATE_PARAGRAPH_MIN_LENGTH,
    max_matches: int = 50,
) -> list[dict]:
    """Flag substantial paragraphs that appear verbatim more than once.

    A common copy-paste mistake in DSA specs is duplicating a boilerplate
    paragraph (a Submittals item, a Quality Assurance clause, etc.). This
    detector finds paragraphs of ``min_length`` characters or more that
    appear at least twice in the same document and reports each occurrence
    after the first so the editor sees every duplicate.

    Intentional scope:
      - Operates on the *content* string, not the paragraph map, so it
        catches both real DOCX paragraphs and any text that the extractor
        merged onto a single line.
      - Skips paragraphs whose stripped text is shorter than ``min_length``.
        This avoids flagging numbered subheadings ("PART 1 - GENERAL") that
        repeat across sections by design.
      - Compares with ``casefold()`` + collapsed whitespace so a duplicate
        with trailing whitespace or capitalization differences still flags.
        The reported ``match`` is the verbatim original text, so the user
        can locate it.
    """
    if not content:
        return []
    seen: dict[str, list[tuple[str, int]]] = {}
    cursor = 0
    for para in content.split("\n\n"):
        # ``cursor`` is the absolute offset of ``para`` in the original
        # content. Bump it by the paragraph length + the 2-char separator
        # we just consumed so subsequent positions stay accurate.
        para_start = cursor
        cursor += len(para) + 2
        stripped = para.strip()
        if len(stripped) < min_length:
            continue
        key = re.sub(r"\s+", " ", stripped).casefold()
        seen.setdefault(key, []).append((stripped, para_start))

    alerts: list[dict] = []
    for occurrences in seen.values():
        if len(occurrences) < 2:
            continue
        # Report each occurrence after the first so users see every dup.
        for original, position in occurrences[1:]:
            preview = original if len(original) <= 140 else original[:140] + "…"
            alerts.append(
                {
                    "filename": filename,
                    "type": "Duplicate paragraph",
                    "match": preview,
                    "context": preview,
                    "position": position,
                    "occurrence_count": len(occurrences),
                    "deterministic_rule": DETERMINISTIC_RULE_DUPLICATE_PARAGRAPH,
                }
            )
            if len(alerts) >= max_matches:
                return alerts
    return alerts


def preprocess_spec(
    content: str,
    filename: str,
    *,
    cycle: Optional[CodeCycle] = None,
) -> PreprocessResult:
    """Run all detection passes on a single specification.

    When ``cycle`` is provided, also run stale code-cycle detection and
    structural checks. ``cycle=None`` skips those passes — callers without
    a cycle still get template-marker, invalid-code-cycle, and
    duplicate-paragraph detection, which never require a cycle (their
    vocabulary comes from the cycle's owning module, degrading to the
    default module when ``cycle`` is ``None``).

    The LEED detector is gated by the module's
    ``detector_vocabulary.flag_leed_references`` — LEED references are
    copy/paste errors for some domains (CA K-12 DSA) and genuine scope for
    others (a data-center module pursuing certification).
    """
    vocabulary = module_for_cycle(cycle).detector_vocabulary
    code_cycle_alerts: list[dict] = []
    if cycle is not None:
        code_cycle_alerts = detect_stale_code_cycle_references(content, filename, cycle)
    structural_alerts = (
        detect_empty_sections(content, filename)
        + detect_duplicate_headings(content, filename)
    )
    leed_alerts: list[dict] = []
    if vocabulary.flag_leed_references:
        leed_alerts = detect_leed_references(content, filename)
    return PreprocessResult(
        leed_alerts=leed_alerts,
        placeholder_alerts=detect_placeholders(content, filename),
        code_cycle_alerts=code_cycle_alerts,
        structural_alerts=structural_alerts,
        template_marker_alerts=detect_unresolved_template_markers(content, filename),
        invalid_code_cycle_alerts=detect_invalid_code_cycle_strings(
            content, filename, vocabulary=vocabulary
        ),
        duplicate_paragraph_alerts=detect_duplicate_paragraphs(content, filename),
    )
