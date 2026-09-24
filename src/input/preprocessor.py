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
    - Placeholders: [INSERT...], [VERIFY...], [TBD], a bare TBD, ___, etc.
      (Unresolved editorial markers that need attention before issuing)
    - Code-cycle citations (stale and invalid years, old ASCE 7 editions),
      section structure (empty sections, duplicate headings), duplicate
      paragraphs, and project-level file-naming consistency.

Usage:
    from preprocessor import preprocess_spec, PreprocessResult
    
    result = preprocess_spec(spec_content, "23 21 13 - Hydronic Piping.docx")
    print(f"Found {len(result.leed_alerts)} LEED references")
    print(f"Found {len(result.placeholder_alerts)} placeholders")
"""
from __future__ import annotations

import re
from collections import Counter
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
    # Wrong-polity token alerts (WS-4, D-15 [FT]): strings suspicious purely
    # as a function of the run's project country (bare "UL listed" on a
    # Canadian run, "O. Reg." citations on a US run). Populated only when
    # ``preprocess_spec`` receives a ``profile_country`` — profile-less runs
    # are byte-identical. Alert dicts additionally carry a ``note`` key (the
    # module rule's explanation, rendered into the report).
    polity_alerts: list[dict] = field(default_factory=list)


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
DETERMINISTIC_RULE_WRONG_POLITY: str = "wrong_polity_token"


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

# Placeholder policy (plan WP-04D):
#
# * A bracketed marker counts only when its keyword is a whole word, so
#   ``[EDITION 2024]`` is not an EDIT placeholder and ``[SELECTED ITEMS]`` is
#   not a SELECT one. ``[OPTION …]``, ``[OPTIONS …]`` and ``[OPTIONAL …]`` all
#   stay placeholders: in the supported templates a bracketed OPTIONAL marks
#   a keep-or-delete choice the specifier still has to make, like
#   ``[SELECT …]``, so the word boundary must not quietly drop it.
# * A bare ``TBD`` in running text is a placeholder too. The bare pattern is
#   last in the list, so a TBD inside a bracketed marker (``[TBD]``,
#   ``[INSERT SIZE TBD]``) is already covered by that marker's span and is not
#   counted twice (``_find_matches`` skips contained spans).
# * A bare TBD that is part of a hyphenated or longer token is an identifier,
#   not a placeholder: ``TBDF-200`` and ``TBD-200`` are both left alone, the
#   same way ``XXX-12`` is a model number to the template-marker rule below.
#   A dash used as punctuation (``TBD - see drawings``, ``TBD—by Architect``)
#   does not join a token, so those still flag. Inside brackets the marker
#   syntax decides instead: ``[TBD-1]`` is a numbered placeholder.
PLACEHOLDER_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)\[\s*INSERT\b[^\]]*\]", "INSERT placeholder"),
    (r"(?i)\[\s*VERIFY\b[^\]]*\]", "VERIFY placeholder"),
    (r"(?i)\[\s*EDIT\b[^\]]*\]", "EDIT placeholder"),
    (r"(?i)\[\s*SELECT\b[^\]]*\]", "SELECT placeholder"),
    (r"(?i)\[\s*COORDINATE\b[^\]]*\]", "COORDINATE placeholder"),
    (r"(?i)\[\s*TO\s+BE\s+DETERMINED\b[^\]]*\]", "TBD placeholder"),
    (r"(?i)\[\s*TBD\b[^\]]*\]", "TBD placeholder"),
    (r"(?i)\[\s*N\/A\b[^\]]*\]", "N/A placeholder"),
    (r"(?i)\[\s*OPTION(?:S|AL)?\b[^\]]*\]", "OPTION placeholder"),
    (r"(?i)<\s*VERIFY\b[^>]*>", "VERIFY tag"),
    (r"(?i)<\s*EDIT\b[^>]*>", "EDIT tag"),
    (r"(?i)<\s*INSERT\b[^>]*>", "INSERT tag"),
    (r"_{3,}", "Underscore placeholder"),
    (r"\[\s*\.\.\.\s*\]", "Ellipsis placeholder"),
    (r"(?i)(?<![\w-])TBD(?!-?\w)", "TBD placeholder"),
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


# Characters that separate the parts of a standard's designation. Word
# autocorrects "7-16" to an en dash, and a pasted designation can carry an em
# dash, a non-breaking hyphen, or a minus sign (plan WP-04C). All of them read
# as the ASCII hyphen.
_DESIGNATION_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_DESIGNATION_SEPARATOR = r"[\s\-" + _DESIGNATION_DASHES + r"]"

# ASCE 7 edition references. Only flag editions older than the cycle's
# nominal ASCE 7 edition (e.g. 7-10 / 7-05 when cycle says 7-22). The
# pattern is structural (engine); the recognition whitelist of real,
# published editions lives on the module vocabulary
# (``asce7_plausible_editions``) so a stray capture like "ASCE 7-42" is
# ignored while every genuine edition older than the cycle's nominal one is
# still flagged (TRUST_AUDIT P2-1).
#
# The designation takes the forms ASCE itself has published it under
# (``ASCE 7-16``, ``ASCE/SEI 7-16``, ``SEI/ASCE 7-02``), with an optional
# ``Standard`` (``ASCE Standard 7-16``), any separator in
# ``_DESIGNATION_SEPARATOR``, and a two- or four-digit edition year
# (``7-16`` / ``7-2016``). ``_asce7_edition_key`` normalizes the year before
# the plausibility and age checks.
_ASCE7_PATTERN = re.compile(
    r"\b(?:ASCE\s*/\s*SEI|SEI\s*/\s*ASCE|ASCE)"
    r"(?:" + _DESIGNATION_SEPARATOR + r"*Standard)?"
    + _DESIGNATION_SEPARATOR + r"*7"
    + _DESIGNATION_SEPARATOR + r"*(\d{4}|\d{2})\b",
    flags=re.IGNORECASE,
)


def _asce7_edition_key(edition: str) -> str | None:
    """The two-digit edition key for a captured ASCE 7 edition year.

    A two-digit capture is already the key (``"16"``). A four-digit one keeps
    its last two digits only when the century agrees with how
    :func:`_asce7_edition_year` widens them — ``"2016"`` is ``"16"`` and
    ``"1998"`` is ``"98"``, but ``"1916"`` names no edition and returns
    ``None``, as does anything that is not two or four digits.
    """
    if len(edition) == 2 and edition.isdigit():
        return edition
    if len(edition) == 4 and edition.isdigit():
        key = edition[2:]
        return key if _asce7_edition_year(key) == int(edition) else None
    return None


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


# How far either side of a stale citation the suppression cues are looked
# for. The window is intentionally small; the sentence and citation bounds
# below usually narrow it further.
_STALE_CYCLE_SUPPRESS_WINDOW: int = 80

# Sentence boundaries that narrow the suppression window on either side of
# a match to the clause the citation sits in. Both scan directions consult
# the same tuple; the trailing scan cuts at the earliest of them by position
# (never by tuple order), see ``_should_suppress_stale_cycle``.
_STALE_CYCLE_SENTENCE_TERMINATORS: tuple[str, ...] = (".", ";", "\n\n")

# ---------------------------------------------------------------------------
# Stale-citation suppression cues (plan WP-04B).
#
# A stale citation is suppressed only when the text around it says the
# CITATION itself is historical or rejected: "the 2019 CBC has been
# superseded", "previously per the 2019 CBC", "shall not follow the 2019 CBC",
# "the prior edition, 2019 CBC". Words that merely sit nearby do not count,
# which is what the older keyword list got wrong: "prior to fabrication",
# "the historical society", and "may not deviate from" all silenced an active
# requirement. A negated requirement to comply ("shall not deviate from 2022
# CBC", "cannot depart from 2022 CBC") is still a requirement, so only a
# closed list of verbs that REJECT the citation (follow, use, apply,
# reference, cite, …) counts as a rejection.
#
# Most cues must touch the citation, with only the "glue" below between
# them: articles and punctuation before it, copulas and relative pronouns
# after it. A few unambiguous historical words (``previously``,
# ``formerly``, ``no longer``, "prior edition") may sit anywhere earlier in
# the citation's clause, unless a present-tense requirement verb (``shall``,
# ``must``, ``will``, ``should``) comes between them and the citation:
# "previously approved submittals shall comply with 2022 CBC" is an active
# requirement about old submittals, not an old requirement.
#
# The clause is also cut at the neighboring citations, so each citation is
# judged by its own context when a sentence cites several: in "the 2019 CBC
# was superseded by the 2022 CBC, which governs", only 2019 is historical.
# ---------------------------------------------------------------------------

_APOSTROPHE = "['\u2019]"  # Word autocorrects ' to a curly apostrophe

# Negated modal / auxiliary: "shall not", "does not", "cannot", "don't", …
_NEGATION = (
    r"(?:(?:shall|should|must|will|would|may|might|can|could|do|does|did)\s+not"
    r"|cannot"
    r"|(?:ca|do|does|did|sha|must|wo|would|should|could)n" + _APOSTROPHE + r"t)"
)

# "the prior edition", "a previous code cycle", "historical versions": an
# edition noun explicitly qualified as old.
_HISTORICAL_EDITION = (
    r"\b(?:previous|prior|earlier|former|older|superseded|outdated|obsolete"
    r"|withdrawn|historical|historic|expired)"
    r"\s+(?:(?:code|model[\s-]code)\s+)?(?:edition|cycle|version|adoption)s?\b"
)

# What may separate a cue from the citation that FOLLOWS it: an article and
# punctuation ("not the 2019 CBC", "the prior edition, 2019 CBC").
_BEFORE_CITATION = r"(?:\s+(?:the|an?))?[\s,:(\[\-\u2013\u2014]*$"

# What may separate the citation from a cue that FOLLOWS it: punctuation and
# copulas / relative pronouns ("2019 CBC, which has been superseded",
# "2019 CBC (superseded)", "the 2019 CBC edition is no longer used").
_AFTER_CITATION = (
    r"[\s,:(\[\-\u2013\u2014]*"
    r"(?:(?:which|that|is|are|was|were|has|have|had|been|being|now|edition)"
    r"\b[\s,]*)*"
)

# Cues that must END right before the citation (searched in the text before
# it; each pattern is anchored at the end with ``_BEFORE_CITATION``).
_STALE_SUPPRESS_BEFORE_ADJACENT: tuple[re.Pattern, ...] = tuple(
    re.compile(source + _BEFORE_CITATION, flags=re.IGNORECASE)
    for source in (
        # Adjectival: "the superseded 2019 CBC", "the prior 2019 CBC".
        # ("superseded by the 2022 CBC" never matches: "by" is not glue, and
        # there the 2022 CBC is the replacement, not the thing replaced.)
        r"\b(?:superseded|withdrawn|obsolete|outdated|expired|repealed|rescinded"
        r"|retired|historical|historic|previous|prior|former|earlier|older)",
        # "prior to the 2022 CBC" — before that code existed.
        r"\bprior\s+to",
        # "instead of the 2019 CBC", "rather than 2019 CBC".
        r"\b(?:instead\s+of|rather\s+than|in\s+lieu\s+of|in\s+place\s+of)",
        # "the 2025 CBC supersedes / replaces the 2022 CBC".
        r"\b(?:supersedes?|superseding|replaces|replacing)",
        # "shall not follow the 2019 CBC", "do not use 2019 CBC".
        r"\b" + _NEGATION + r"\s+(?:follow|use|apply|reference|cite"
        r"|rely\s+(?:on|upon)|be\s+(?:based\s+on|governed\s+by"
        r"|designed\s+(?:to|per|under)))",
        # "comply with the 2025 CBC, not the 2022 CBC" — a contrast set off
        # by a comma. Bare "not" is not a cue: "work not per 2022 CBC shall
        # be removed" requires the 2022 CBC.
        r",\s*not(?:\s+(?:per|under))?",
        r"\bnot\s+the",
    )
)

# Cues that may sit anywhere earlier in the citation's clause segment. They
# lose their force when a requirement verb stands between them and the
# citation (``_REQUIREMENT_VERB``).
_STALE_SUPPRESS_BEFORE_IN_CLAUSE: tuple[re.Pattern, ...] = (
    # "Previously, the 2022 CBC applied" / "was previously permitted under
    # the 2022 CBC". "As previously specified" points back into the document,
    # not to an old code, so it is not a cue.
    re.compile(r"(?<!\bas\s)\b(?:previously|formerly)\b", flags=re.IGNORECASE),
    re.compile(r"\bno\s+longer\b", flags=re.IGNORECASE),
    re.compile(_HISTORICAL_EDITION, flags=re.IGNORECASE),
)

# A present-tense requirement between an in-clause cue and the citation.
_REQUIREMENT_VERB = re.compile(r"\b(?:shall|must|will|should)\b", flags=re.IGNORECASE)

# Cues that must START right after the citation (matched at the start of the
# text after it, past ``_AFTER_CITATION``).
_STALE_SUPPRESS_AFTER: tuple[re.Pattern, ...] = tuple(
    re.compile(_AFTER_CITATION + source, flags=re.IGNORECASE)
    for source in (
        # "2019 CBC has been superseded", "2019 CBC (withdrawn)".
        r"(?:superseded|withdrawn|obsolete|outdated|expired|repealed|rescinded"
        r"|retired)\b",
        # "2022 CBC is no longer used", "which is no longer the adopted edition".
        r"no\s+longer\b",
        # "the 2019 CBC, previously in effect", "2019 CBC (formerly adopted)".
        r"(?:previously|formerly)\b",
        # "2019 CBC is not applicable", "is not the current edition",
        # "isn't in effect".
        r"(?:not|(?:is|are|was|were)n" + _APOSTROPHE + r"t)\s+(?:the\s+)?"
        r"(?:applicable|adopted|enforced|in\s+effect|in\s+force|current|valid"
        r"|governing)\b",
        # "2022 CBC does not apply", "2019 CBC shall not be used".
        _NEGATION + r"\s+(?:apply|govern|be\s+(?:used|applied|followed"
        r"|referenced|cited|enforced))\b",
        r"not\s+to\s+be\s+(?:used|applied|followed|referenced|cited)\b",
        # "2019 CBC, the previous edition", "2019 CBC was the prior cycle".
        r"(?:(?:the|an?)\s+)?" + _HISTORICAL_EDITION,
    )
)


def _cue_before_citation(pre_window: str) -> bool:
    """True when the text before a citation marks it historical or rejected."""
    if any(pattern.search(pre_window) for pattern in _STALE_SUPPRESS_BEFORE_ADJACENT):
        return True
    for pattern in _STALE_SUPPRESS_BEFORE_IN_CLAUSE:
        for cue in pattern.finditer(pre_window):
            if not _REQUIREMENT_VERB.search(pre_window, cue.end()):
                return True
    return False


def _cue_after_citation(post_window: str) -> bool:
    """True when the text after a citation marks it historical or rejected."""
    return any(pattern.match(post_window) for pattern in _STALE_SUPPRESS_AFTER)


def _should_suppress_stale_cycle(
    content: str,
    match_start: int,
    match_end: int,
    *,
    window_start: int = 0,
    window_end: int | None = None,
) -> bool:
    """Return True when a stale citation is described as historical or rejected.

    Looks at most ``_STALE_CYCLE_SUPPRESS_WINDOW`` characters to either side
    of the match, cut to the clause the citation sits in (``.``, ``;``,
    ``\\n\\n``) and, when the caller passes them, to ``window_start`` /
    ``window_end`` — the end of the previous citation and the start of the
    next one, so a cue about a neighboring citation is never borrowed. The
    cues are described above ``_STALE_SUPPRESS_BEFORE_ADJACENT``.
    """
    if not content:
        return False
    pre_start = max(0, window_start, match_start - _STALE_CYCLE_SUPPRESS_WINDOW)
    pre_window = content[pre_start:match_start]
    # Restrict the *preceding* window to the current sentence so a
    # negation in a previous clause doesn't suppress the active one.
    # Applying every terminator in turn leaves the text after the LAST
    # terminator of any kind.
    for term in _STALE_CYCLE_SENTENCE_TERMINATORS:
        cut = pre_window.rfind(term)
        if cut >= 0:
            pre_window = pre_window[cut + len(term):]
    post_end = min(len(content), match_end + _STALE_CYCLE_SUPPRESS_WINDOW)
    if window_end is not None:
        post_end = min(post_end, window_end)
    post_window = content[match_end:post_end]
    # Same for the *trailing* window: stop at the EARLIEST terminator of
    # any kind. The cut has to be the minimum position, not the first
    # terminator found in tuple order — checking ``"."`` before ``";"``
    # and stopping there kept the ``;``-separated clause in
    # "2019 CBC; the prior edition is no longer referenced. Provide…" and
    # suppressed an active citation on the strength of a negation that
    # belongs to the next clause.
    cuts = [
        cut
        for cut in (post_window.find(term) for term in _STALE_CYCLE_SENTENCE_TERMINATORS)
        if cut >= 0
    ]
    if cuts:
        post_window = post_window[: min(cuts)]
    return _cue_before_citation(pre_window) or _cue_after_citation(post_window)


def _citation_bounds(
    citations: list[tuple[int, int]], start: int, end: int, length: int
) -> tuple[int, int]:
    """The end of the citation before ``start`` and the start of the one after ``end``.

    ``citations`` are the spans of every citation-shaped match in the text
    (any year, any edition). Overlapping spans are the same citation found by
    another pattern and are skipped.
    """
    before = max((c_end for c_start, c_end in citations if c_end <= start), default=0)
    after = min((c_start for c_start, c_end in citations if c_start >= end), default=length)
    return before, after


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

    The detector is intentionally narrow: it never flags the cycle's own year,
    and it skips an old citation its own clause describes as historical or
    rejected ("previously per the 2019 CBC", "the 2019 CBC has been
    superseded", "shall not follow the 2019 CBC") — see
    ``_should_suppress_stale_cycle``. Callers can downgrade alerts by
    post-processing the returned dicts; this function does not call the API.
    """
    if not cycle:
        return []
    vocabulary = module_for_cycle(cycle).detector_vocabulary
    target_year = (cycle.primary_code_year or "").strip()
    if not target_year:
        return []

    plausible_years = frozenset(vocabulary.plausible_cycle_years)
    code_patterns = _stale_cycle_patterns_for(vocabulary)
    # Every citation-shaped span in the text, whatever its year or edition:
    # the suppression window of one citation stops at its neighbors, so a
    # sentence citing two codes judges each by its own context.
    citations = sorted(
        {match.span() for pattern in code_patterns for match in pattern.finditer(content)}
        | {match.span() for match in _ASCE7_PATTERN.finditer(content)}
    )
    alerts: list[dict] = []
    seen_spans: list[tuple[int, int]] = []
    for pattern in code_patterns:
        for match in pattern.finditer(content):
            year = next((g for g in match.groups() if g and g in plausible_years), None)
            if year is None or year == target_year:
                continue
            span = (match.start(), match.end())
            if any(s <= span[0] and span[1] <= e for s, e in seen_spans):
                continue
            # Skip citations their own clause describes as historical or
            # rejected. Recorded spans still get tracked above so a
            # suppressed match doesn't bleed into the overlap dedup for
            # downstream patterns.
            window_start, window_end = _citation_bounds(
                citations, span[0], span[1], len(content)
            )
            if _should_suppress_stale_cycle(
                content, span[0], span[1],
                window_start=window_start, window_end=window_end,
            ):
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
            # "7-2016" and "7-16" are the same edition: normalize to the
            # two-digit key first, so the whitelist and the age check see one
            # form. Century-aware comparison: a 1998 edition is older than
            # 2022 even though ``98 > 22`` numerically. Unknown captures (not
            # a real edition) are ignored to avoid flagging stray numbers.
            edition = _asce7_edition_key(match.group(1))
            if (
                edition is None
                or edition not in vocabulary.asce7_plausible_editions
                or _asce7_edition_year(edition) >= target_asce_year
            ):
                continue
            span = (match.start(), match.end())
            if any(s <= span[0] and span[1] <= e for s, e in seen_spans):
                continue
            # Same suppression for ASCE 7 — a sentence that explicitly
            # says "no longer use ASCE 7-10" is descriptive, not a
            # requirement.
            window_start, window_end = _citation_bounds(
                citations, span[0], span[1], len(content)
            )
            if _should_suppress_stale_cycle(
                content, span[0], span[1],
                window_start=window_start, window_end=window_end,
            ):
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


# ---------------------------------------------------------------------------
# Section structure (plan WP-04A).
#
# The empty-section and duplicate-heading checks share one reading of the
# heading hierarchy, ``heading_candidates``. A paragraph is a heading only
# when its number AND its title are heading-shaped:
#
# * The number is ``PART n`` (level 0) or a dotted CSI article number:
#   ``1.01`` / ``1.1`` (level 1) or ``1.01.1`` (level 2). A bare integer is
#   never a heading number. "2 coats of primer", "12 inches minimum" and
#   "1 year from Substantial Completion" are quantities, and a SectionFormat
#   PART heading always carries the word PART.
# * The title reads as a title, not as the rest of a sentence: its first
#   letter is a capital ("1.5 inches minimum cover" continues a sentence), it
#   has no ``shall`` / ``must`` (a requirement is prose even in capitals), it
#   does not end a mixed-case sentence with a period, it is not a table row
#   (the extractor joins cells with " | "), and it is at most 120 characters.
#
# Anything else is body text. A heading's content is its whole subtree —
# everything up to the next heading at the same or a higher level — so a
# PART whose articles have text is not empty. The structure also stops at an
# ``END OF SECTION`` line and at the extractor's footnote, endnote, and
# header/footer blocks, which follow the body and are never a heading's
# content.
# ---------------------------------------------------------------------------

#: How a heading's number is known. ``"typed"``: the number is literal text in
#: the paragraph ("1.01 SUMMARY"). Word's automatic numbering is not read yet
#: (plan WP-03, chunk S14). When it is, its labels need a provenance of their
#: own, because a label Word generates is not text an edit can change.
HEADING_PROVENANCE_TYPED: str = "typed"


@dataclass(frozen=True)
class HeadingCandidate:
    """One qualified heading in the spec body, as the structural checks read it.

    Attributes:
        number: The heading number, upper-cased with whitespace collapsed:
            ``"PART 1"``, ``"1.01"``, ``"2.3.1"``.
        title: The heading title with a trailing colon removed (``"SUMMARY"``).
        level: 0 for a PART, 1 for an article (``1.01``), 2 for a sub-article
            (``1.01.1``).
        start: Offset of the number in the content — the alert ``position``.
        end: Offset just past the heading line.
        run_in: The heading line carries its own text after a colon
            ("1.03 REFERENCES: ASTM A53"), so it is never empty.
        provenance: Where the number came from; see
            ``HEADING_PROVENANCE_TYPED``.
    """

    number: str
    title: str
    level: int
    start: int
    end: int
    run_in: bool = False
    provenance: str = HEADING_PROVENANCE_TYPED

    @property
    def label(self) -> str:
        """The heading as alerts quote it (``"1.01 SUMMARY"``)."""
        return f"{self.number} {self.title}"


# A numbered line at the start of a paragraph (preceded by the paragraph
# delimiter "\n\n" or the start of the text). The number and title must be in
# the same paragraph (a line break between them is allowed, a paragraph break
# is not), and the title ends at the end of its line; ``_heading_title_shaped``
# then decides whether the line is a heading.
_HEADING_LINE_RE = re.compile(
    r"(?:^|\n\n)\s*"
    r"(?P<num>PART[^\S\n]+\d+|\d+(?:\.\d+){1,2})"
    r"(?:[^\S\n]|\n(?!\n))+"
    r"(?P<title>[^\n]+)",
    flags=re.IGNORECASE,
)

_HEADING_TITLE_MAX_CHARS: int = 120

# A requirement verb makes a line a sentence, whatever its capitalization.
_REQUIREMENT_IN_TITLE_RE = re.compile(r"\b(?:shall|must)\b", flags=re.IGNORECASE)

# Where the heading structure stops: an "END OF SECTION" line, or the
# extractor's footnote / endnote / header-footer block delimiter (see
# ``extractor.extract_text_from_docx``; the text-box block is left out on
# purpose, because a text box is anchored in the body and can hold a heading's
# only content). Each closes every open heading; headings after an
# "END OF SECTION" line (a second section in the same file) are read afresh.
_STRUCTURE_END_RE = re.compile(
    r"(?:^|\n\n)[^\S\n]*(?P<stop>END\s+OF\s+SECTION\b"
    r"|===== (?:FOOTNOTE|ENDNOTE|HEADER/FOOTER) CONTENT =====)",
    flags=re.IGNORECASE,
)


def _heading_title_shaped(title: str) -> bool:
    """True when ``title`` reads as a heading title rather than prose."""
    if not title or len(title) > _HEADING_TITLE_MAX_CHARS or "|" in title:
        return False
    letters = [ch for ch in title if ch.isalpha()]
    if not letters or letters[0].islower():
        return False
    if _REQUIREMENT_IN_TITLE_RE.search(title):
        return False
    if title.endswith((".", "!", "?")) and any(ch.islower() for ch in letters):
        return False
    return True


def heading_candidates(content: str) -> list[HeadingCandidate]:
    """The qualified section headings in ``content``, in document order.

    See the rules above ``HeadingCandidate``. Every heading-dependent check
    reads the document through this one function, so a line that is not a
    heading for the empty-section check is not one for the duplicate check
    either.
    """
    candidates: list[HeadingCandidate] = []
    for match in _HEADING_LINE_RE.finditer(content):
        raw_title = match.group("title").strip()
        if not _heading_title_shaped(raw_title):
            continue
        title = raw_title.rstrip(":").strip()
        if not title:
            continue
        number = re.sub(r"\s+", " ", match.group("num").strip()).upper()
        level = 0 if number.startswith("PART") else number.count(".")
        _, colon, after_colon = raw_title.partition(":")
        candidates.append(
            HeadingCandidate(
                number=number,
                title=title,
                level=level,
                start=match.start("num"),
                end=match.end("title"),
                run_in=bool(colon and after_colon.strip()),
            )
        )
    return candidates


def _structure_ends(content: str) -> list[int]:
    """Offsets where the heading structure stops (see ``_STRUCTURE_END_RE``)."""
    return [match.start("stop") for match in _STRUCTURE_END_RE.finditer(content)]


def _subtree_end(
    candidates: list[HeadingCandidate], index: int, structure_ends: list[int], length: int
) -> int:
    """Where heading ``index``'s subtree ends: its next sibling or ancestor,
    the next structure end, or the end of the text — whichever comes first."""
    heading = candidates[index]
    end = length
    for later in candidates[index + 1:]:
        if later.level <= heading.level:
            end = later.start
            break
    for stop in structure_ends:
        if stop > heading.start:
            return min(end, stop)
    return end


def _subtree_has_content(
    content: str, candidates: list[HeadingCandidate], index: int, end: int
) -> bool:
    """True when any text in heading ``index``'s subtree is not a heading line."""
    heading = candidates[index]
    if heading.run_in:
        return True
    cursor = heading.end
    for descendant in candidates[index + 1:]:
        if descendant.start >= end:
            break
        if content[cursor:descendant.start].strip() or descendant.run_in:
            return True
        cursor = descendant.end
    return bool(content[cursor:end].strip())


def detect_empty_sections(
    content: str,
    filename: str,
    *,
    max_matches: int = 50,
) -> list[dict]:
    """Flag headings whose whole subtree has no content.

    A heading is empty when nothing in its subtree — its own body and every
    descendant's — is text other than heading lines. This catches templated
    DSA specs where an editor deleted the body without removing the heading
    scaffold, while a PART whose articles have text is not empty.

    Alerts do not repeat: when a PART and all of its articles are empty, the
    PART is reported and its articles are not. Put generally, an empty
    heading is reported only when its parent is not empty (or it has none),
    so every empty heading is covered by exactly one alert. Alerts come in
    document order, at most ``max_matches``.
    """
    candidates = heading_candidates(content)
    if not candidates:
        return []
    structure_ends = _structure_ends(content)
    ends = [
        _subtree_end(candidates, i, structure_ends, len(content))
        for i in range(len(candidates))
    ]
    empty = [
        not _subtree_has_content(content, candidates, i, ends[i])
        for i in range(len(candidates))
    ]
    alerts: list[dict] = []
    for i, heading in enumerate(candidates):
        if not empty[i]:
            continue
        parent = next(
            (
                j
                for j in range(i - 1, -1, -1)
                if candidates[j].level < heading.level
            ),
            None,
        )
        if parent is not None and ends[parent] > heading.start and empty[parent]:
            continue  # reported through the empty ancestor
        ctx_start = max(0, heading.start - 40)
        ctx_end = min(len(content), ends[i] + 40)
        alerts.append(
            {
                "filename": filename,
                "type": "Empty section",
                "match": heading.label,
                "context": content[ctx_start:ctx_end].replace("\n", " ").strip(),
                "position": heading.start,
                "section_number": heading.number,
                "section_title": heading.title,
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
    Only qualified headings count (``heading_candidates``), so a quantity
    line repeated in the body is not a duplicate heading.
    """
    counts: dict[str, list[HeadingCandidate]] = {}
    for heading in heading_candidates(content):
        counts.setdefault(heading.number, []).append(heading)

    alerts: list[dict] = []
    for number, occurrences in counts.items():
        if len(occurrences) < 2:
            continue
        # Report each occurrence after the first so users see every duplicate.
        for heading in occurrences[1:]:
            ctx_start = max(0, heading.start - 60)
            ctx_end = min(len(content), heading.start + 120)
            alerts.append(
                {
                    "filename": filename,
                    "type": "Duplicate section heading",
                    "match": heading.label,
                    "context": content[ctx_start:ctx_end].replace("\n", " ").strip(),
                    "position": heading.start,
                    "section_number": number,
                    "occurrence_count": len(occurrences),
                    "deterministic_rule": DETERMINISTIC_RULE_DUPLICATE_HEADING,
                }
            )
            if len(alerts) >= max_matches:
                return alerts
    return alerts


# CSI-style file names (plan WP-04E). The six-digit section number leads the
# name, written separated ("21 05 00", "21-05-00") or compact ("210500"),
# optionally after the word SECTION ("SECTION 21 13 16.DOCX",
# "Section 211316.docx"). The extension and its case play no part. A name
# that does not lead with a section number ("Fire Protection Narrative.docx",
# "2024-05-01 Addendum 2.docx", "NFPA 13 Checklist.docx") has no CSI style, so
# it is left out of the comparison instead of outvoting the names that have
# one. Whether such a file can be routed or reviewed is a separate question
# this informational notice does not answer.
_CSI_FILENAME_RE = re.compile(
    r"^\s*(?P<prefix>SECTION[\s_-]*)?"
    r"(?:\d{2}\s*(?P<sep>[\s-])\s*\d{2}\s*(?P=sep)\s*\d{2}|\d{6})"
    r"(?![0-9A-Za-z])",
    flags=re.IGNORECASE,
)

# Human-readable name of each number style; a SECTION prefix is added after it.
_CSI_NUMBER_STYLE_LABELS: dict[str, str] = {
    "space": "space-separated",
    "dash": "dash-separated",
    "compact": "compact",
}
_SECTION_PREFIXED_STYLE = "section-"


def _csi_filename_style(filename: str) -> str | None:
    """The CSI naming style of ``filename``, or ``None`` when it has none.

    Styles are ``"space"``, ``"dash"``, and ``"compact"``, each with a
    ``"section-"`` variant for a SECTION-prefixed name.
    """
    match = _CSI_FILENAME_RE.match(filename)
    if not match:
        return None
    separator = match.group("sep")
    if separator is None:
        style = "compact"
    else:
        style = "space" if separator.isspace() else "dash"
    return _SECTION_PREFIXED_STYLE + style if match.group("prefix") else style


def _csi_filename_style_label(style: str) -> str:
    """``"space"`` -> ``"space-separated"``, ``"section-compact"`` ->
    ``"compact with a SECTION prefix"``."""
    if style.startswith(_SECTION_PREFIXED_STYLE):
        base = style[len(_SECTION_PREFIXED_STYLE):]
        return f"{_CSI_NUMBER_STYLE_LABELS[base]} with a SECTION prefix"
    return _CSI_NUMBER_STYLE_LABELS[style]


def detect_inconsistent_file_naming(filenames: list[str]) -> list[dict]:
    """Project-level (cross-file) check for mixed CSI naming conventions.

    Only names that lead with a CSI section number take part (see
    ``_CSI_FILENAME_RE``). When they share one style there is nothing to
    report. When one style is used by more than half of them, it is the
    project's style and every name in another style gets an
    ``"Inconsistent CSI filename style (expected …)"`` alert. When no style
    has that majority, no convention is invented: every CSI-named file gets
    a neutral ``"Mixed CSI filename styles (no dominant style)"`` alert
    (``dominant_style`` is ``None``) that lists the styles in use.

    Alerts come in input order, one per file at most. Used by the
    GUI/pipeline to warn before submission. No model tokens are spent.
    """
    styles: dict[str, str] = {}
    for fname in filenames:
        style = _csi_filename_style(fname)
        if style is not None:
            styles.setdefault(fname, style)
    counts = Counter(styles.values())
    if len(counts) < 2:
        return []
    dominant, dominant_count = counts.most_common(1)[0]
    if dominant_count * 2 > len(styles):
        dominant_label = _csi_filename_style_label(dominant)
        return [
            {
                "filename": fname,
                "type": f"Inconsistent CSI filename style (expected {dominant_label})",
                "match": fname,
                "context": (
                    f"{fname} — {_csi_filename_style_label(style)}; "
                    f"most files are {dominant_label}"
                ),
                "position": 0,
                "dominant_style": dominant,
                "found_style": style,
                "deterministic_rule": DETERMINISTIC_RULE_INCONSISTENT_FILENAME,
            }
            for fname, style in styles.items()
            if style != dominant
        ]
    summary = ", ".join(
        f"{count} {_csi_filename_style_label(style)}" for style, count in counts.most_common()
    )
    return [
        {
            "filename": fname,
            "type": "Mixed CSI filename styles (no dominant style)",
            "match": fname,
            "context": (
                f"{fname} — {_csi_filename_style_label(style)}; "
                f"no single style dominates ({summary})"
            ),
            "position": 0,
            "dominant_style": None,
            "found_style": style,
            "deterministic_rule": DETERMINISTIC_RULE_INCONSISTENT_FILENAME,
        }
        for fname, style in styles.items()
    ]


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

# Paragraphs the extractor synthesizes from surfaces outside ``<w:body>``
# (section headers / footers, text boxes, footnotes, endnotes) carry a
# bracketed label prefix. Their repetition is structural, not editorial — a
# page header is emitted once per document section, so every multi-section
# spec would otherwise flag its own running header as a copy-paste defect on
# every run and carry that noise into the review prompt. The label set
# mirrors the prefixes ``extractor.extract_text_from_docx`` emits; the match
# is anchored at the paragraph start so a body paragraph that merely
# mentions "[Header]" is still eligible.
_SYNTHETIC_PARAGRAPH_PREFIX_RE = re.compile(
    r"^\[(?:Header|Footer|Text Box|Footnote [^\]]*|Endnote [^\]]*)\] "
)


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
      - Skips extractor-synthesized entries (``[Header] …``, ``[Footer] …``,
        ``[Text Box] …``, ``[Footnote n] …``, ``[Endnote n] …``): a running
        header repeats once per section by construction, not by mistake.
        Extraction output itself is untouched — only the detector ignores
        these lines.
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
        if _SYNTHETIC_PARAGRAPH_PREFIX_RE.match(stripped):
            # Extractor-synthesized entries repeat by construction (a page
            # header is emitted once per document section) and are not
            # copy-paste defects — see ``_SYNTHETIC_PARAGRAPH_PREFIX_RE``.
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


@lru_cache(maxsize=16)
def _compiled_polity_rules(rules: tuple, country: str) -> tuple:
    """Compile a module's polity rules for one country. Cached per module.

    ``rules`` is the module's hashable ``polity_suspect_tokens`` tuple;
    registration already verified every pattern compiles, so failures here
    are impossible in practice (the compile is repeated for defense).
    """
    return tuple(
        (re.compile(rule.pattern), rule)
        for rule in rules
        if rule.country == country
    )


def detect_wrong_polity_tokens(
    content: str,
    filename: str,
    *,
    rules: tuple,
    country: str,
    max_matches: int = 100,
) -> list[dict]:
    """Flag tokens suspicious purely as a function of the project country.

    WS-4, design D-15 [FT]: the field trial's largest single finding class
    (≈10 of 52) needed no model call to *flag* — bare ``UL listed`` /
    ``NFPA 70`` / ``OSHA`` on a ``country=CA`` run; ``NBC`` / ``O. Reg.`` /
    ``CRN`` citations on a ``country=US`` run. The model phrases the fix;
    this detector guarantees the flag. Each alert carries the rule's
    ``note`` explaining the suspicion, rendered into ``<pre_detected>`` and
    the report's alerts section.
    """
    alerts: list[dict] = []
    for compiled, rule in _compiled_polity_rules(tuple(rules), country):
        for match in compiled.finditer(content):
            m_start, m_end = match.start(), match.end()
            ctx_start = max(0, m_start - 60)
            ctx_end = min(len(content), m_end + 60)
            window = content[ctx_start:ctx_end].replace("\n", " ").strip()
            alerts.append(
                {
                    "filename": filename,
                    "type": "Wrong-polity token",
                    "match": match.group(0),
                    # The note leads the context so the generic alert
                    # renderer (which shows only ``context``) surfaces the
                    # WHY alongside the WHERE.
                    "context": f"'{match.group(0)}' — {rule.note} | …{window}…",
                    "position": m_start,
                    "deterministic_rule": DETERMINISTIC_RULE_WRONG_POLITY,
                    "note": rule.note,
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
    profile_country: str | None = None,
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

    ``profile_country`` (WS-4, D-15) activates the wrong-polity token
    detector with the module's country-matched rules. ``None`` — every
    profile-less run — produces byte-identical output (invariant 2).
    """
    module = module_for_cycle(cycle)
    vocabulary = module.detector_vocabulary
    polity_alerts: list[dict] = []
    if profile_country and module.polity_suspect_tokens:
        polity_alerts = detect_wrong_polity_tokens(
            content,
            filename,
            rules=module.polity_suspect_tokens,
            country=profile_country,
        )
    code_cycle_alerts: list[dict] = []
    # Stale-cycle detection is suppressed for a location-aware module (one of
    # the three coupled surfaces in CLAUDE.md's "Edition authority" section;
    # ``TestSurfacesAgree`` asserts the coupling). The detector compares a cited
    # year against the module's own ``primary_code_year`` — one code family,
    # one target — and on these modules that target is an assumption, not an
    # established adoption for the project. A spec correctly citing the 2021
    # IBC in a 2021-IBC jurisdiction would be flagged stale against a pinned
    # 2024, and the hyperscale program covers Canada too, whose governing
    # adoption this single-family detector cannot express at all.
    #
    # Suppression is the intended default here, not a degraded path: the review
    # prompt still receives the research text and still instructs deference on
    # edition questions, so the question reaches a model that can weigh it —
    # which a regex comparing two integers cannot. It must also land WITH the
    # verifier-prompt correction, never after: a ``<pre_detected>`` alert primes
    # the review model, so a firing detector plus an authority-corrected prompt
    # would send contradictory signals into the same request.
    suppress_stale_cycle = getattr(module, "project_profile_enabled", False)
    if cycle is not None and not suppress_stale_cycle:
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
        polity_alerts=polity_alerts,
    )
