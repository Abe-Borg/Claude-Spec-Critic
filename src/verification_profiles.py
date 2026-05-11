"""Verification profiles by issue type (Chunk H).

Before this module, every verification call used the same search budget,
same domain policy, and same prompt — only severity modulated
``max_uses``. That is the wrong axis to vary on: a CRITICAL finding
about an *internal contradiction* (where both sides are quoted from the
spec itself) does not need any web search, while a MEDIUM finding about
a *manufacturer model number* needs manufacturer/datasheet sources, and
a HIGH finding about a *California Title 24 amendment* needs DSA/HCAI
authorities before anything else.

Chunk H Directive 5 calls for profiles that classify the finding by
*kind* and then route search depth, preferred-source language, and (in
future chunks) routing rules from there. Severity remains a modifier —
the profile sets the ceiling for how many searches the model may
reasonably need, severity just nudges within that ceiling.

The classifier is deliberately keyword-based rather than LLM-driven:

- It runs on every finding before verification, so it has to be cheap.
- The signal in the finding text (``codeReference``, ``issue``,
  ``existingText``, ``replacementText``) is usually unambiguous.
- A wrong classification at worst routes the model to a slightly
  different ``max_uses``; the grounding invariant in
  :func:`src.source_grounding.validate_cited_sources` is the real
  safety net.

Public surface:

- :class:`VerificationProfile` — the small closed enum.
- :func:`classify_finding_profile` — pure function over a ``Finding``.
- :func:`profile_max_uses` — search budget for ``(profile, severity)``.
- :func:`profile_label` — short string used in reports/diagnostics.
- :func:`profile_priority_domains` — the authoritative-source guidance
  appended to the verifier system prompt for the chosen profile.
- :func:`profile_web_search_required` — False for the internal-
  coordination profile (where web search adds no signal); True
  otherwise.
"""
from __future__ import annotations

from dataclasses import dataclass
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


# Short human-readable labels for reports / diagnostics. Keep these
# stable; they appear in the diagnostics text output.
_PROFILE_LABELS: dict[VerificationProfile, str] = {
    VerificationProfile.CODE_STANDARD: "Code / Standard",
    VerificationProfile.CALIFORNIA_AHJ: "California / AHJ",
    VerificationProfile.MANUFACTURER: "Manufacturer / Product",
    VerificationProfile.CONSTRUCTABILITY: "Constructability",
    VerificationProfile.INTERNAL_COORDINATION: "Internal Coordination",
}


def profile_label(profile: VerificationProfile | str | None) -> str:
    """Return the human-readable label for ``profile``.

    Accepts the enum, its string value, or ``None`` (returns ``""``).
    Unknown strings round-trip unchanged so future profiles flowing in
    from cached entries still render legibly.
    """
    if profile is None:
        return ""
    if isinstance(profile, VerificationProfile):
        return _PROFILE_LABELS[profile]
    # String — accept either the enum value or an already-pretty label.
    try:
        return _PROFILE_LABELS[VerificationProfile(profile)]
    except (KeyError, ValueError):
        return str(profile)


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
# Profile sets the ceiling; severity is a modifier within the profile.
# Internal-coordination findings get a tiny budget because they should
# not be using web search at all (the upstream pipeline routes them to
# ``local_skip`` when that flag is enabled; the budget here is a
# defense in depth).

@dataclass(frozen=True)
class _ProfileBudget:
    max_uses_critical: int
    max_uses_high: int
    max_uses_medium: int
    max_uses_gripes: int


# Reasonable starting points. Severity ordering within a profile stays
# monotonic: CRITICAL >= HIGH >= MEDIUM >= GRIPES.
_PROFILE_BUDGETS: dict[VerificationProfile, _ProfileBudget] = {
    VerificationProfile.CODE_STANDARD: _ProfileBudget(7, 7, 5, 3),
    VerificationProfile.CALIFORNIA_AHJ: _ProfileBudget(8, 7, 5, 3),
    VerificationProfile.MANUFACTURER: _ProfileBudget(6, 5, 4, 3),
    VerificationProfile.CONSTRUCTABILITY: _ProfileBudget(5, 5, 4, 3),
    VerificationProfile.INTERNAL_COORDINATION: _ProfileBudget(2, 2, 1, 1),
}


def profile_max_uses(
    profile: VerificationProfile | str | None,
    severity: str | None,
) -> int:
    """Return the web_search ``max_uses`` budget for ``(profile, severity)``.

    Unknown profiles fall back to ``CONSTRUCTABILITY`` because that is
    the most permissive non-extreme bucket. Unknown severities fall
    back to the ``MEDIUM`` row of the chosen profile.
    """
    if isinstance(profile, str):
        try:
            profile = VerificationProfile(profile)
        except ValueError:
            profile = VerificationProfile.CONSTRUCTABILITY
    if not isinstance(profile, VerificationProfile):
        profile = VerificationProfile.CONSTRUCTABILITY
    budget = _PROFILE_BUDGETS[profile]
    sev = (severity or "").strip().upper()
    if sev == "CRITICAL":
        return budget.max_uses_critical
    if sev == "HIGH":
        return budget.max_uses_high
    if sev == "GRIPES":
        return budget.max_uses_gripes
    # MEDIUM and anything we don't recognize.
    return budget.max_uses_medium


def profile_web_search_required(
    profile: VerificationProfile | str | None,
) -> bool:
    """Whether web search is meaningful for this profile.

    Returns ``False`` only for ``INTERNAL_COORDINATION`` — the verifier
    can still attach the web_search tool (the model is allowed to
    self-route), but the calling pipeline can use this as a cheap gate
    to skip web verification entirely. Today only the diagnostics path
    consults this; the verifier itself defers to
    :mod:`verification_router` for the local-skip decision so the two
    code paths agree on which findings bypass web search.
    """
    if isinstance(profile, str):
        try:
            profile = VerificationProfile(profile)
        except ValueError:
            return True
    return profile is not VerificationProfile.INTERNAL_COORDINATION


# Authoritative-source language emitted into the verifier system
# prompt. Keep this in stable-prefix space so prompt-cache breakpoints
# do not invalidate per finding. Only the profile-specific paragraph
# changes between calls; the rest of the system prompt is byte-for-
# byte identical across profiles, so the cache prefix still fires for
# any two findings of the same profile.

_PROFILE_PROMPT_GUIDANCE: dict[VerificationProfile, str] = {
    VerificationProfile.CALIFORNIA_AHJ: (
        "Verification profile: California / AHJ. Prefer California regulatory\n"
        "authorities (dgs.ca.gov, dsa.ca.gov, hcai.ca.gov, bsc.ca.gov,\n"
        "energy.ca.gov) and the current California-amended code editions.\n"
        "Treat model-code (CBC/CMC/CPC/CEC) sources as authoritative only\n"
        "when the cited section matches the active California amendment;\n"
        "otherwise fall back to UNVERIFIED."
    ),
    VerificationProfile.CODE_STANDARD: (
        "Verification profile: Code / Standard. Prefer code-publisher\n"
        "primary sources (up.codes, codes.iccsafe.org) and standards-body\n"
        "domains (nfpa.org, ashrae.org, iapmo.org, smacna.org, astm.org,\n"
        "asce.org) over secondary commentary. Confirm both the section\n"
        "number and the edition cited in the finding."
    ),
    VerificationProfile.MANUFACTURER: (
        "Verification profile: Manufacturer / Product. Prefer the\n"
        "manufacturer's own technical data pages and listing-agency\n"
        "databases (ul.com, fmglobal.com). When a datasheet contradicts\n"
        "a regulatory source, treat the regulatory source as authoritative."
    ),
    VerificationProfile.CONSTRUCTABILITY: (
        "Verification profile: Constructability. Prefer authoritative\n"
        "industry references (mcaa.org, smacna.org, ashrae.org) for\n"
        "general technical claims. Broad web search is acceptable when\n"
        "tier-1 sources do not address the claim."
    ),
    VerificationProfile.INTERNAL_COORDINATION: (
        "Verification profile: Internal Coordination. This finding is\n"
        "expected to be verifiable from the spec text alone — quote the\n"
        "contradictory passages or placeholder text and return UNVERIFIED\n"
        "rather than searching the web."
    ),
}


def profile_priority_domains(
    profile: VerificationProfile | str | None,
) -> str:
    """Return the authoritative-source guidance paragraph for a profile.

    Empty string for unknown profiles so the verifier system prompt
    degrades gracefully when an operator wires in a future profile
    that this module does not yet know about.
    """
    if isinstance(profile, str):
        try:
            profile = VerificationProfile(profile)
        except ValueError:
            return ""
    if profile is None:
        return ""
    return _PROFILE_PROMPT_GUIDANCE.get(profile, "")
