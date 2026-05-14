"""Verification profiles by issue type.

The classifier groups findings by *kind* (California / code-standard /
manufacturer / constructability / internal-coordination) so the verifier
can attach profile-specific authoritative-source guidance to its system
prompt. Web-search budget is severity-based and identical across
profiles — see :func:`profile_max_uses`.

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
- :func:`classify_finding_profile` — pure function over a ``Finding``.
- :func:`profile_max_uses` — severity-based search budget (profile arg
  is accepted for call-site compatibility but ignored).
"""
from __future__ import annotations

from enum import Enum


class VerificationProfile(str, Enum):
    """Kind of factual claim the verification call is checking.

    Inheriting from ``str`` means ``VerificationProfile.CODE_STANDARD ==
    "code_standard"``, which is convenient for serialization to caches,
    resume state, and diagnostics — no enum-name lookup gymnastics
    required.
    """

    CODE_STANDARD = "code_standard"
    """Generic code / standard / industry-spec verification (CBC, NFPA,
    ASHRAE, IAPMO, ASTM, etc.). Default for code-citing findings without
    California-specific keywords."""

    CALIFORNIA_AHJ = "california_ahj"
    """California-specific code, Title 24 amendments, DSA / HCAI / OSHPD
    requirements. These need California regulatory authorities first."""

    MANUFACTURER = "manufacturer"
    """Manufacturer model numbers, datasheets, listings, listed-product
    checks. Search depth needs to cover manufacturer technical data."""

    CONSTRUCTABILITY = "constructability"
    """Generic technical / constructability claim that is not tied to a
    specific code section or product. Broader web search appropriate."""

    INTERNAL_COORDINATION = "internal_coordination"
    """Finding is internally verifiable from the spec text alone — an
    internal contradiction, a formatting issue, a placeholder, a typo,
    or a duplicate. Web search adds no signal."""


# Keyword sets per profile. Order matters: classification checks
# California first (so a "CBC + DSA" finding becomes CALIFORNIA_AHJ,
# not CODE_STANDARD), then manufacturer, then code/standard, then
# internal-coordination, with constructability as the last-resort
# default for findings that have substantive issue text but no clear
# kind signal.

_CALIFORNIA_KEYWORDS = (
    "california",
    "calif.",
    "dsa",
    "dgs",
    "hcai",
    "oshpd",
    "title 24",
    "title-24",
    "bsc.ca.gov",
    "ca.gov",
    "calgreen",
    "cal green",
    "cec ",
    "cbsc",
    "ahj",
    "authority having jurisdiction",
)

_CODE_STANDARD_KEYWORDS = (
    "cbc",
    "cmc",
    "cpc",
    "cec",
    "nfpa",
    "asme",
    "ashrae",
    "ieee",
    "iapmo",
    "astm",
    "ansi",
    "smacna",
    "ul ",
    "ul-",
    "ul listed",
    "code section",
    "standard",
    "energy code",
    "fire code",
    "plumbing code",
    "mechanical code",
    "building code",
    "electrical code",
    "asce",
)

_MANUFACTURER_KEYWORDS = (
    "manufacturer",
    "model number",
    "model no",
    "datasheet",
    "data sheet",
    "submittal",
    "catalog",
    "trane",
    "carrier",
    "york",
    "daikin",
    "greenheck",
    "victaulic",
    "watts",
    "zurn",
    "kohler",
    "american standard",
    "viega",
    "uponor",
    "pex",
    "listed product",
    "factory authorized",
    "approved equivalent",
    "equal to",
    "or approved equal",
)

_INTERNAL_COORDINATION_KEYWORDS = (
    "internal contradiction",
    "internally contradicts",
    "contradiction within",
    "duplicate paragraph",
    "duplicate heading",
    "duplicate section",
    "placeholder",
    "tbd",
    "[select]",
    "[verify]",
    "[insert",
    "formatting",
    "typo",
    "typographical",
    "leed",
    "missing placeholder",
    "self-referen",  # "self-referential", "self-references"
    "inconsistent within",
)


def _haystack(finding) -> str:
    """Build the lowercased text we run keyword detection on.

    We include ``codeReference`` because it carries the most reliable
    signal (e.g. a non-empty ``codeReference`` strongly suggests
    CODE_STANDARD or CALIFORNIA_AHJ, never INTERNAL_COORDINATION). We
    join with newlines so substring matches do not span field
    boundaries spuriously.
    """
    parts = []
    for attr in ("codeReference", "issue", "existingText", "replacementText", "section"):
        value = getattr(finding, attr, None)
        if value:
            parts.append(str(value))
    return "\n".join(parts).lower()


def classify_finding_profile(finding) -> VerificationProfile:
    """Pure-function classifier from a Finding to a VerificationProfile.

    Decision order:

    1. Internal-coordination keywords (placeholder/LEED/typo/duplicate/
       internal contradiction) → ``INTERNAL_COORDINATION``. This is
       checked first because findings with these signals never need
       external grounding regardless of any other text. The
       :mod:`verification_router` ``local_skip`` classifier already
       handles the *GRIPES* subset of these; the profile classifier
       extends the same logic to higher-severity findings so the
       verifier's web-search ``max_uses`` is throttled even when
       ``local_skip`` is disabled.
    2. California / AHJ keywords → ``CALIFORNIA_AHJ`` (precedence over
       generic code-standard, since California amendments add
       constraints to model codes).
    3. Manufacturer keywords → ``MANUFACTURER``.
    4. Code / standard keywords or non-empty ``codeReference`` →
       ``CODE_STANDARD``.
    5. Default → ``CONSTRUCTABILITY``.

    Empty / missing fields default to ``CONSTRUCTABILITY``.
    """
    if finding is None:
        return VerificationProfile.CONSTRUCTABILITY
    text = _haystack(finding)
    if not text:
        return VerificationProfile.CONSTRUCTABILITY

    # 1. Internal coordination — checked first.
    if any(kw in text for kw in _INTERNAL_COORDINATION_KEYWORDS):
        return VerificationProfile.INTERNAL_COORDINATION

    # 2. California / AHJ.
    if any(kw in text for kw in _CALIFORNIA_KEYWORDS):
        return VerificationProfile.CALIFORNIA_AHJ

    # 3. Manufacturer.
    if any(kw in text for kw in _MANUFACTURER_KEYWORDS):
        return VerificationProfile.MANUFACTURER

    # 4. Code / standard. ``codeReference`` is the most reliable signal —
    # a finding that names a code section is by definition a code claim.
    code_ref = (getattr(finding, "codeReference", None) or "").strip()
    if code_ref:
        return VerificationProfile.CODE_STANDARD
    if any(kw in text for kw in _CODE_STANDARD_KEYWORDS):
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


