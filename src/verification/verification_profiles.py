"""Verification profiles by issue type.

The classifier groups findings by *kind* (jurisdictional / code-standard /
manufacturer / constructability / internal-coordination) so the verifier
can attach profile-specific authoritative-source guidance to its system
prompt. Web-search budget is severity-based and identical across
profiles — see :func:`profile_max_uses`.

The classifier *logic* (precedence order, the ``codeReference`` signal, the
constructability default) is engine-owned here; the keyword *vocabulary* is
module data (:class:`~src.modules.base.ProfileKeywords` on the owning
:class:`ReviewModule`) so a non-California module classifies against its own
jurisdiction and product terms.

The classifier is keyword-based rather than LLM-driven:

- It runs on every finding before verification, so it has to be cheap.
- The signal in the finding text (``codeReference``, ``issue``,
  ``existingText``, ``replacementText``) is usually unambiguous.
- A wrong classification at worst picks the wrong priority-source
  paragraph; the grounding invariant in
  :func:`src.source_grounding.validate_cited_sources` is the real
  safety net.

Public surface:

- :class:`VerificationProfile` — the small closed enum.
- :func:`parse_verification_profile` — string → enum with legacy-value
  mapping (pre-rename ``"california_ahj"`` rows in caches / resume state
  keep resolving).
- :func:`classify_finding_profile` — pure function over a ``Finding`` and
  an optional keyword vocabulary.
- :func:`matches_any_keyword` / :func:`compile_keyword_patterns` — the
  whole-word keyword matcher shared with :mod:`verification_prescreen`
  (the edge rules are documented under "Whole-word keyword matching").
- :func:`profile_max_uses` — severity-based search budget (profile arg
  is accepted for call-site compatibility but ignored).
"""
from __future__ import annotations

import functools
import re
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..modules import ProfileKeywords


class VerificationProfile(str, Enum):
    """Kind of factual claim the verification call is checking.

    Inheriting from ``str`` means ``VerificationProfile.CODE_STANDARD ==
    "code_standard"``, which is convenient for serialization to caches,
    resume state, and diagnostics — no enum-name lookup gymnastics
    required.
    """

    CODE_STANDARD = "code_standard"
    """Generic code / standard / industry-spec verification (base codes,
    NFPA, ASHRAE, IAPMO, ASTM, etc.). Default for code-citing findings
    without jurisdiction-specific keywords."""

    JURISDICTIONAL = "jurisdictional"
    """Jurisdiction-specific code, amendments, and authority-having-
    jurisdiction requirements (California / Title 24 / DSA / HCAI for the
    CA module; fire marshal / insurer criteria for others). These need the
    jurisdiction's regulatory authorities first. Formerly
    ``california_ahj`` — :func:`parse_verification_profile` maps the
    legacy value."""

    MANUFACTURER = "manufacturer"
    """Manufacturer model numbers, datasheets, listings, listed-product
    checks. Search depth needs to cover manufacturer technical data."""

    CONSTRUCTABILITY = "constructability"
    """Generic technical / constructability claim that is not tied to a
    specific code section or product. Broader web search appropriate."""

    INTERNAL_COORDINATION = "internal_coordination"
    """Finding is internally verifiable from the spec text alone — an
    internal contradiction, a placeholder, a typo, or a duplicate. Web
    search adds no signal. ``"formatting"`` is deliberately *not* a
    trigger: a real code formatting requirement ("label valves per ASME
    A13.1 color formatting") must not be routed away from grounding."""


# Pre-rename profile values that may survive in persisted state (cached
# verification results, resume-state routing decisions, trace payloads).
# Parsed back to the current enum so a legacy row never crashes the wave
# parser or silently degrades to CONSTRUCTABILITY.
_LEGACY_PROFILE_VALUES: dict[str, VerificationProfile] = {
    "california_ahj": VerificationProfile.JURISDICTIONAL,
}


def parse_verification_profile(
    value: object,
    default: VerificationProfile = VerificationProfile.CONSTRUCTABILITY,
) -> VerificationProfile:
    """Parse a stored profile string, mapping legacy values.

    Unknown / missing values fall back to ``default`` — the same degrade
    posture the routing deserializer used before the profile rename.
    """
    if isinstance(value, VerificationProfile):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    legacy = _LEGACY_PROFILE_VALUES.get(text)
    if legacy is not None:
        return legacy
    try:
        return VerificationProfile(text)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Whole-word keyword matching
# ---------------------------------------------------------------------------
#
# Shared by this classifier and the local-skip prescreen so both keyword
# surfaces agree on what "mentions" means. Bare substring membership was the
# previous contract, and it fired whenever a keyword appeared as the tail or
# head of a longer word: ``"leed"`` on "bleed valve" / "bleed line",
# ``"watts"`` on "kilowatts", ``"abb"`` on "abbreviation", ``"sel-"`` on
# "diesel-driven", ``"ul-"`` on "full-height". Each keyword now compiles to a
# case-insensitive regex under these rules:
#
# * A word boundary (``\b``) is anchored at an edge only when that edge
#   character is a word character. ``"[select]"``, ``"calif."``, ``"ul-"``,
#   ``"???"`` keep their literal punctuation edges and match exactly as they
#   did; ``"leed"`` no longer matches inside "bleed".
# * A trailing *letter* edge tolerates a plain English plural (``s``):
#   ``"standard"`` still matches "standards", ``"placeholder"`` still
#   matches "placeholders", ``"submittal"`` "submittals". The lists are
#   written in the singular and substring matching absorbed the plural for
#   free; a bare boundary would have silently dropped every one of them.
#   A trailing digit edge (``"nfpa 70"``, ``"est4"``) takes a plain boundary.
# * Whitespace in a keyword — internal or at an edge — matches any
#   whitespace run (``\s+``), so ``"internal contradiction"`` matches
#   across a line break or a doubled space, and ``"cec "`` / ``"ul "`` keep
#   requiring the trailing separator that keeps them off "cecil" / "bulk".
# * A keyword ending in :data:`KEYWORD_PREFIX_MARKER` (``*``) is an
#   open-ended stem: the marker is stripped and no trailing boundary is
#   applied, so ``"self-referen*"`` matches "self-referential" and
#   "self-references" and ``"typo*"`` matches "typographical". This is the
#   only way to opt a keyword out of the trailing boundary; use it when a
#   keyword is deliberately a fragment.
#
# Compiled patterns are cached per keyword and per keyword tuple (module
# vocabularies and the prescreen lists are hashable tuples), so the
# per-finding hot path never recompiles.

KEYWORD_PREFIX_MARKER = "*"
"""Trailing marker declaring a keyword as an open-ended stem (no trailing
word boundary). See "Whole-word keyword matching" above."""

_WORD_CHAR = re.compile(r"\w")


def _is_word_char(ch: str) -> bool:
    return bool(_WORD_CHAR.fullmatch(ch))


@functools.lru_cache(maxsize=None)
def compile_keyword_pattern(keyword: str) -> "re.Pattern[str]":
    """Compile one routing keyword to its whole-word regex.

    The edge rules are documented under "Whole-word keyword matching"
    above. Raises ``ValueError`` for a keyword with no non-whitespace
    content (a bare ``"*"`` included) — such a keyword would otherwise
    compile to a pattern that matches almost any text.
    """
    if not isinstance(keyword, str):
        raise TypeError(
            f"routing keyword must be a str, got {type(keyword).__name__}"
        )
    open_ended = keyword.endswith(KEYWORD_PREFIX_MARKER)
    stem = keyword[: -len(KEYWORD_PREFIX_MARKER)] if open_ended else keyword
    if not stem.strip():
        raise ValueError(
            f"routing keyword needs at least one non-whitespace character: "
            f"{keyword!r}"
        )
    # Whitespace runs (internal or at an edge) become ``\s+``; ``re.split``
    # yields an empty first/last part for edge whitespace, which escapes to
    # nothing and leaves the ``\s+`` in place.
    body = r"\s+".join(re.escape(part) for part in re.split(r"\s+", stem))
    lead = r"\b" if _is_word_char(stem[0]) else ""
    if open_ended:
        tail = ""
    elif stem[-1].isalpha():
        tail = r"s?\b"
    elif _is_word_char(stem[-1]):
        tail = r"\b"
    else:
        tail = ""
    return re.compile(lead + body + tail, re.IGNORECASE)


@functools.lru_cache(maxsize=None)
def compile_keyword_patterns(
    keywords: tuple[str, ...],
) -> "tuple[re.Pattern[str], ...]":
    """Compile a keyword tuple once; cached on the (hashable) tuple."""
    return tuple(compile_keyword_pattern(keyword) for keyword in keywords)


def matches_any_keyword(text: str, keywords) -> bool:
    """Return True iff ``text`` contains any keyword as a whole word.

    ``keywords`` is normally one of the tuples on a module's
    :class:`ProfileKeywords` or a prescreen list; any iterable of strings
    is accepted and coerced to a tuple for the cache key.
    """
    if not text:
        return False
    if not isinstance(keywords, tuple):
        keywords = tuple(keywords)
    return any(
        pattern.search(text) for pattern in compile_keyword_patterns(keywords)
    )


def _default_keywords() -> "ProfileKeywords":
    """Keyword vocabulary used when a caller has no module context.

    Degrades to the default module — the same posture as
    ``module_for_cycle(None)``. Imported lazily so this module can be
    imported before the registry finishes validating.
    """
    from ..modules import DEFAULT_MODULE

    return DEFAULT_MODULE.profile_keywords


def _haystack(finding) -> str:
    """Build the lowercased text we run keyword detection on.

    We include ``codeReference`` because it carries the most reliable
    signal (e.g. a non-empty ``codeReference`` strongly suggests
    CODE_STANDARD or JURISDICTIONAL, never INTERNAL_COORDINATION). Fields
    are joined with newlines. Keyword tests are whole-word
    (:func:`matches_any_keyword`); a multi-word keyword's internal
    whitespace matches any whitespace run, so the newline is a
    readability boundary rather than a hard matching one.
    """
    parts = []
    for attr in ("codeReference", "issue", "existingText", "replacementText", "section"):
        value = getattr(finding, attr, None)
        if value:
            parts.append(str(value))
    return "\n".join(parts).lower()


def classify_finding_profile(
    finding,
    *,
    keywords: "ProfileKeywords | None" = None,
) -> VerificationProfile:
    """Pure-function classifier from a Finding to a VerificationProfile.

    ``keywords`` is the owning module's vocabulary; ``None`` degrades to
    the default module's (cycle-bearing callers thread it via
    ``select_routing(cycle=...)``).

    Decision order:

    1. Internal-coordination keywords (placeholder/typo/duplicate/
       internal contradiction) → ``INTERNAL_COORDINATION``. This is
       checked first because findings with these signals never need
       external grounding regardless of any other text. The
       :mod:`verification_prescreen` ``local_skip`` classifier already
       handles the *GRIPES* subset of these; the profile classifier
       extends the same logic to higher-severity findings so the
       verifier's web-search ``max_uses`` is throttled even when
       ``local_skip`` is disabled.
    2. Jurisdictional keywords → ``JURISDICTIONAL`` (precedence over
       generic code-standard, since jurisdiction amendments add
       constraints to model codes).
    3. Manufacturer keywords → ``MANUFACTURER``.
    4. Code / standard keywords or non-empty ``codeReference`` →
       ``CODE_STANDARD``.
    5. Default → ``CONSTRUCTABILITY``.

    Every keyword test is whole-word via :func:`matches_any_keyword`, so a
    keyword never fires as the tail of a longer word ("bleed" does not
    match ``"leed"``). Empty / missing fields default to
    ``CONSTRUCTABILITY``.
    """
    if finding is None:
        return VerificationProfile.CONSTRUCTABILITY
    text = _haystack(finding)
    if not text:
        return VerificationProfile.CONSTRUCTABILITY

    vocabulary = keywords if keywords is not None else _default_keywords()

    # 1. Internal coordination — checked first.
    if matches_any_keyword(text, vocabulary.internal_coordination):
        return VerificationProfile.INTERNAL_COORDINATION

    # 2. Jurisdictional / AHJ.
    if matches_any_keyword(text, vocabulary.jurisdictional):
        return VerificationProfile.JURISDICTIONAL

    # 3. Manufacturer.
    if matches_any_keyword(text, vocabulary.manufacturer):
        return VerificationProfile.MANUFACTURER

    # 4. Code / standard. ``codeReference`` is the most reliable signal —
    # a finding that names a code section is by definition a code claim.
    code_ref = (getattr(finding, "codeReference", None) or "").strip()
    if code_ref:
        return VerificationProfile.CODE_STANDARD
    if matches_any_keyword(text, vocabulary.code_standard):
        return VerificationProfile.CODE_STANDARD

    # 5. Default.
    return VerificationProfile.CONSTRUCTABILITY


# ---------------------------------------------------------------------------
# Search-budget policy
# ---------------------------------------------------------------------------
#
# Flat severity-based budget — the same ceiling applies to every profile.
# The grounding invariant + internal-coordination prompt guidance are the
# safeguards that prevent low-signal findings from wasting their budget;
# we don't carve a separate budget tier per kind. The actual map lives in
# :mod:`api_config` so the web-search tool builder and the verifier read
# from one source.


def profile_max_uses(
    profile: VerificationProfile | str | None,
    severity: str | None,
) -> int:
    """Return the web_search ``max_uses`` budget for ``severity``.

    Profile is accepted for call-site compatibility but does not affect
    the budget — every profile shares the same severity-based ceiling.
    """
    del profile
    from ..core.api_config import web_search_max_uses_for_severity
    return web_search_max_uses_for_severity(severity)
