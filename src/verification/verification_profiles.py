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
- :func:`profile_max_uses` — severity-based search budget (profile arg
  is accepted for call-site compatibility but ignored).
"""
from __future__ import annotations

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
    internal contradiction, a formatting issue, a placeholder, a typo,
    or a duplicate. Web search adds no signal."""


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
    CODE_STANDARD or JURISDICTIONAL, never INTERNAL_COORDINATION). We
    join with newlines so substring matches do not span field
    boundaries spuriously.
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

    Empty / missing fields default to ``CONSTRUCTABILITY``.
    """
    if finding is None:
        return VerificationProfile.CONSTRUCTABILITY
    text = _haystack(finding)
    if not text:
        return VerificationProfile.CONSTRUCTABILITY

    vocabulary = keywords if keywords is not None else _default_keywords()

    # 1. Internal coordination — checked first.
    if any(kw in text for kw in vocabulary.internal_coordination):
        return VerificationProfile.INTERNAL_COORDINATION

    # 2. Jurisdictional / AHJ.
    if any(kw in text for kw in vocabulary.jurisdictional):
        return VerificationProfile.JURISDICTIONAL

    # 3. Manufacturer.
    if any(kw in text for kw in vocabulary.manufacturer):
        return VerificationProfile.MANUFACTURER

    # 4. Code / standard. ``codeReference`` is the most reliable signal —
    # a finding that names a code section is by definition a code claim.
    code_ref = (getattr(finding, "codeReference", None) or "").strip()
    if code_ref:
        return VerificationProfile.CODE_STANDARD
    if any(kw in text for kw in vocabulary.code_standard):
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
