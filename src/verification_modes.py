"""Verification modes and model routing (Chunk I).

Before this module, the pieces that decide *how* a finding gets verified
lived in five different places:

- Keyword local-skip classifier (:mod:`verification_router`).
- Optional Haiku triage classifier (:mod:`triage`).
- Sonnet-default + Opus-escalation logic
  (:mod:`verification_router.should_escalate_verification`).
- Profile-aware web_search ``max_uses`` ceiling
  (:mod:`verification_profiles.profile_max_uses`).
- Model-aware ``thinking`` config (:mod:`api_config.apply_thinking_config`).

Each piece is sensible in isolation, but the system as a whole had no
single answer to "what *kind* of verification is this finding getting?"
That made it impossible to surface a routing decision in logs or
reports, and made it easy to accidentally use the deepest path
everywhere — every non-local-skip call defaulted to Sonnet + adaptive
thinking + full profile budget, even for a GRIPES-severity placeholder
finding.

Chunk I formalizes the four modes the plan calls out and wires them
into a single routing function. Each mode is a closed bundle of
``(model_family, thinking_enabled, search_budget_multiplier,
allows_escalation)`` so a future tuning pass can adjust the budget
shape for one mode without touching the others.

Public surface:

- :class:`VerificationMode` — the four-value enum.
- :class:`ModePolicy` — frozen dataclass holding the per-mode policy
  knobs.
- :func:`select_verification_mode` — pure-function router from a
  ``Finding`` (+ context) to a :class:`VerificationMode`.
- :func:`mode_policy` — table lookup.
- :func:`mode_label` — pretty label for reports / diagnostics.
- :func:`mode_search_budget` — applies the per-mode multiplier on top
  of the profile/severity ceiling.

Design rules:

- **Deterministic routing.** The router is a pure function over the
  finding text + a few booleans (escalated, classifier verdict). No
  LLM is consulted to pick a mode — the goal is for the routing
  decision to be reproducible and inspectable.
- **Backward-compatible defaults.** Today's default behavior is
  STANDARD_REASONING for most findings: Sonnet + adaptive thinking +
  full profile budget. STRICT_STRUCTURED narrows the search budget
  for GRIPES-severity findings (where deep reasoning is overkill);
  DEEP_REASONING is the explicit name for the existing escalation
  path. LOCAL_SKIP is the name for what the keyword classifier and
  Haiku triage already do.
- **Modes do not bypass the cache.** Routing happens *after* the
  cache lookup and *after* the keyword / Haiku triage local-skip
  decision; the cache and local-skip path already short-circuit
  before any web-verification call is built.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .api_config import (
    MODEL_OPUS_47,
    MODEL_SONNET_46,
    VERIFICATION_ESCALATION_MODEL,
    VERIFICATION_MODEL_DEFAULT,
    verification_sonnet_default_enabled,
)
from .verification_profiles import VerificationProfile, classify_finding_profile


class VerificationMode(str, Enum):
    """How a finding is being verified.

    Inheriting from ``str`` means ``VerificationMode.LOCAL_SKIP ==
    "local_skip"``, which is convenient for serialization to caches,
    resume state, diagnostics, and JSON exports — no enum-name lookup
    gymnastics required.
    """

    LOCAL_SKIP = "local_skip"
    """Deterministic / locally classified. No remote call is made.
    Used for placeholder GRIPES, internal-contradiction findings the
    keyword or Haiku classifier resolved, and cache misses for
    findings the routing pre-pass deemed unverifiable from web
    sources."""

    STRICT_STRUCTURED = "strict_structured"
    """Cheap, narrow verification for simple factual / editorial
    claims. Sonnet, ``thinking`` disabled, search budget scaled down
    from the profile ceiling. Used for GRIPES-severity findings that
    pass the keyword classifier (e.g. a GRIPES with a non-empty
    ``codeReference`` — local-skip would not catch it, but it does
    not need deep reasoning either)."""

    STANDARD_REASONING = "standard_reasoning"
    """The default for substantive technical claims. Sonnet,
    ``thinking`` enabled, full profile-aware search budget. This is
    the mode that most CODE_STANDARD / MANUFACTURER / CALIFORNIA_AHJ
    /CONSTRUCTABILITY findings of MEDIUM and above ride."""

    DEEP_REASONING = "deep_reasoning"
    """Opus + adaptive thinking + full profile budget. Reserved for
    CRITICAL CALIFORNIA_AHJ findings (where the initial pass jumps
    straight to Opus) and for escalation re-runs of CRITICAL / HIGH
    findings that the standard pass could not ground. Terminal — a
    deep-reasoning result does not escalate further."""


# Short human-readable labels for reports / diagnostics. Stable on
# purpose: they appear in diagnostics text output and serialized
# verification records.
_MODE_LABELS: dict[VerificationMode, str] = {
    VerificationMode.LOCAL_SKIP: "Local skip",
    VerificationMode.STRICT_STRUCTURED: "Strict structured",
    VerificationMode.STANDARD_REASONING: "Standard reasoning",
    VerificationMode.DEEP_REASONING: "Deep reasoning",
}


def mode_label(mode: VerificationMode | str | None) -> str:
    """Return the human-readable label for ``mode``.

    Accepts the enum, its string value, or ``None`` (returns ``""``).
    Unknown strings round-trip unchanged so future modes flowing in
    from cached entries still render legibly.
    """
    if mode is None:
        return ""
    if isinstance(mode, VerificationMode):
        return _MODE_LABELS[mode]
    try:
        return _MODE_LABELS[VerificationMode(mode)]
    except (KeyError, ValueError):
        return str(mode)


# ---------------------------------------------------------------------------
# Mode policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModePolicy:
    """Per-mode policy bundle.

    The fields capture every decision the verifier needs to make
    differently per mode. Each field is independently testable; the
    table at the bottom of this module is the single source of truth
    so a tuning change touches one map.

    Attributes
    ----------
    mode:
        Identity of the mode this policy describes. Exposed so
        callers can store the policy and still answer "which mode am
        I?" without a side channel.
    model:
        Default Anthropic model id for this mode. The verifier may
        still override with an explicit ``model=`` keyword (operator
        overrides, escalation paths, tests), but the default flows
        from here.
    thinking_enabled:
        Whether the verifier should request ``thinking`` on this
        call. ``False`` for LOCAL_SKIP (which makes no remote call
        anyway) and STRICT_STRUCTURED (cheap, narrow). The Haiku-
        based triage classifier has its own no-thinking rule via
        :data:`api_config._PHASES_NO_THINKING`; this flag is the
        per-mode complement for non-triage paths.
    search_budget_multiplier:
        Multiplier applied on top of the per-(profile, severity)
        ``max_uses`` ceiling from :mod:`verification_profiles`. 1.0
        means "use the full profile budget"; 0.5 means "give this
        mode half"; 0.0 means "no web search" (LOCAL_SKIP). Floor of
        1 is applied at use-site so a multiplier > 0 always allows
        at least one search.
    web_search_enabled:
        Whether the request should attach the web_search tool. Only
        ``False`` for LOCAL_SKIP; everything else attaches it (even
        STRICT_STRUCTURED — the floor of 1 ensures the model can
        still verify a single factual claim).
    allows_escalation:
        Whether a failed verification in this mode is eligible to
        escalate. ``False`` for LOCAL_SKIP (terminal), STRICT_STRUCTURED
        (low-stakes), and DEEP_REASONING (already at the top of the
        ladder). Only STANDARD_REASONING escalates, and only when
        :func:`verification_router.should_escalate_verification`
        agrees.
    """

    mode: VerificationMode
    model: str
    thinking_enabled: bool
    search_budget_multiplier: float
    web_search_enabled: bool
    allows_escalation: bool


def _default_initial_model() -> str:
    """The model used by STANDARD_REASONING's initial pass.

    Reads through :data:`VERIFICATION_MODEL_DEFAULT` so the
    ``SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT=0`` toggle (which
    flips the default verifier to Opus-everywhere) still flows
    through to the mode policy. The result is recomputed at each
    call so a test that monkeypatches the env var picks up the
    change without reloading this module.
    """
    return VERIFICATION_MODEL_DEFAULT


def _deep_reasoning_model() -> str:
    """The model used by DEEP_REASONING.

    Always the escalation model. When Sonnet-default is disabled the
    initial verifier is already Opus, but DEEP_REASONING still names
    the Opus escalation model explicitly so the routing decision is
    legible.
    """
    return VERIFICATION_ESCALATION_MODEL


def mode_policy(mode: VerificationMode | str) -> ModePolicy:
    """Return the policy record for ``mode``.

    Unknown / malformed strings fall back to STANDARD_REASONING so a
    pre-existing finding that did not record its mode (e.g. legacy
    cache entries from before Chunk I) still produces a sensible
    request shape. The default is intentionally the same shape the
    verifier used before Chunk I — same model, same thinking
    config, same budget — so a missing mode is observationally
    indistinguishable from the pre-Chunk-I behavior.
    """
    if isinstance(mode, str):
        try:
            mode = VerificationMode(mode)
        except ValueError:
            mode = VerificationMode.STANDARD_REASONING
    if not isinstance(mode, VerificationMode):
        mode = VerificationMode.STANDARD_REASONING

    if mode is VerificationMode.LOCAL_SKIP:
        return ModePolicy(
            mode=mode,
            model="local",
            thinking_enabled=False,
            search_budget_multiplier=0.0,
            web_search_enabled=False,
            allows_escalation=False,
        )
    if mode is VerificationMode.STRICT_STRUCTURED:
        # Sonnet by default. Even when SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT=0
        # flips the initial verifier to Opus, STRICT_STRUCTURED stays on the
        # cheaper model — the whole point of the mode is "use a cheaper /
        # narrower path for findings that do not need deep reasoning."
        return ModePolicy(
            mode=mode,
            model=MODEL_SONNET_46,
            thinking_enabled=False,
            # Half-budget. Floor-of-1 is applied at use-site, so a profile
            # whose ceiling is 1-3 still gets at least one search.
            search_budget_multiplier=0.5,
            web_search_enabled=True,
            allows_escalation=False,
        )
    if mode is VerificationMode.DEEP_REASONING:
        return ModePolicy(
            mode=mode,
            model=_deep_reasoning_model(),
            thinking_enabled=True,
            search_budget_multiplier=1.0,
            web_search_enabled=True,
            # Already at the top of the ladder.
            allows_escalation=False,
        )
    # STANDARD_REASONING — the default.
    return ModePolicy(
        mode=VerificationMode.STANDARD_REASONING,
        model=_default_initial_model(),
        thinking_enabled=True,
        search_budget_multiplier=1.0,
        web_search_enabled=True,
        allows_escalation=True,
    )


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
#
# The router is pure-function over a small set of inputs:
#
#   1. ``finding`` — used to read severity + classify the profile.
#   2. ``local_skip`` — True iff the keyword classifier or Haiku triage
#      already decided the finding can be locally resolved. Caller passes
#      this in rather than re-running the classifier so the caller stays
#      in control of the local-skip feature flag.
#   3. ``escalated`` — True iff this call is the second pass after a
#      failed STANDARD_REASONING attempt. Forces DEEP_REASONING.
#   4. ``cached_mode`` — when the cache returns a hit, the caller passes
#      the stored mode so the returned record carries the original
#      routing decision instead of being silently relabeled.
#
# Rules, in priority order:
#
#   1. ``local_skip`` → LOCAL_SKIP. Highest priority — if the upstream
#      classifier said "no web verification needed," nothing in this
#      module should override that.
#   2. ``escalated`` → DEEP_REASONING. The escalation path is reserved
#      for CRITICAL/HIGH UNVERIFIED reruns; once we're escalating, we're
#      committing to Opus regardless of what severity or profile would
#      have picked initially.
#   3. CRITICAL + CALIFORNIA_AHJ → DEEP_REASONING. The initial pass
#      jumps straight to Opus for CRITICAL California-specific claims
#      because the ambiguity surface is large enough that a Sonnet pass
#      will usually escalate anyway, and skipping a wasted call is a
#      direct cost win.
#   4. GRIPES (any profile that is not INTERNAL_COORDINATION) →
#      STRICT_STRUCTURED. GRIPES are editorial / cosmetic / placeholder
#      style findings; the local-skip classifier catches most of them,
#      and the ones that slip through (typically with a non-empty
#      codeReference) do not need deep reasoning. Save the budget.
#   5. INTERNAL_COORDINATION (non-GRIPES) → STRICT_STRUCTURED. The
#      local-skip classifier only catches GRIPES; a HIGH-severity
#      internal contradiction still falls through here. The profile
#      classifier already throttled the search budget to 1-2 — match
#      that with the cheaper mode.
#   6. Default → STANDARD_REASONING.


def select_verification_mode(
    finding,
    *,
    local_skip: bool = False,
    escalated: bool = False,
    cached_mode: VerificationMode | str | None = None,
) -> VerificationMode:
    """Select the :class:`VerificationMode` for a finding.

    See the module docstring for the rule order. Returns a
    :class:`VerificationMode`; callers should pass it to
    :func:`mode_policy` to get the actual policy bundle.
    """
    # 0. Cache hit — preserve the stored mode if it was recorded. A
    # legacy cache entry without a mode falls through to the regular
    # routing rules; that is what we want, because the entry will be
    # re-tagged with its current mode the next time it's used.
    if cached_mode is not None:
        if isinstance(cached_mode, VerificationMode):
            return cached_mode
        try:
            return VerificationMode(cached_mode)
        except (TypeError, ValueError):
            pass  # fall through

    # 1. Local skip wins outright.
    if local_skip:
        return VerificationMode.LOCAL_SKIP

    # 2. Escalation forces DEEP_REASONING regardless of severity/profile.
    if escalated:
        return VerificationMode.DEEP_REASONING

    if finding is None:
        return VerificationMode.STANDARD_REASONING

    severity = (getattr(finding, "severity", None) or "").strip().upper()
    profile = classify_finding_profile(finding)

    # 3. Critical California/AHJ goes straight to deep reasoning. The
    # initial Sonnet pass for these almost always escalates anyway —
    # the ambiguity surface (Title 24 amendments, DSA / HCAI nuance,
    # local AHJ interpretation) is wide enough that Opus is the right
    # first call.
    if severity == "CRITICAL" and profile is VerificationProfile.CALIFORNIA_AHJ:
        # Only fire this rule if escalation is actually wired up. When
        # Sonnet-default is disabled, the initial model is already Opus
        # and there is no distinct "deep" tier — STANDARD_REASONING is
        # the correct label for that configuration.
        if verification_sonnet_default_enabled():
            return VerificationMode.DEEP_REASONING

    # 4. GRIPES → strict structured. Internal-coordination GRIPES are
    # already caught by local-skip when that's on; this rule catches
    # GRIPES with a non-empty ``codeReference`` and any GRIPES seen
    # while local-skip is disabled.
    if severity == "GRIPES":
        return VerificationMode.STRICT_STRUCTURED

    # 5. Non-GRIPES internal-coordination findings — match the
    # profile classifier's tight budget with a cheap mode.
    if profile is VerificationProfile.INTERNAL_COORDINATION:
        return VerificationMode.STRICT_STRUCTURED

    # 6. Default.
    return VerificationMode.STANDARD_REASONING


# ---------------------------------------------------------------------------
# Search-budget application
# ---------------------------------------------------------------------------


def mode_search_budget(
    mode: VerificationMode | str,
    *,
    profile_ceiling: int,
) -> int:
    """Apply the per-mode multiplier to a profile/severity ceiling.

    LOCAL_SKIP returns 0 (web search is disabled for that mode).
    Everything else returns ``max(1, round(ceiling * multiplier))``
    so a non-zero multiplier always grants at least one search.

    The caller computes the profile/severity ceiling via
    :func:`verification_profiles.profile_max_uses`; this helper only
    handles the mode-level scaling so the two policies compose
    cleanly.
    """
    policy = mode_policy(mode)
    if policy.search_budget_multiplier <= 0.0 or not policy.web_search_enabled:
        return 0
    if profile_ceiling <= 0:
        return 0
    scaled = profile_ceiling * policy.search_budget_multiplier
    # ``round(half_to_even)`` is fine here — the multipliers are 0.5 / 1.0
    # in practice, so the rounding choice has no observable effect.
    return max(1, int(round(scaled)))


__all__ = [
    "VerificationMode",
    "ModePolicy",
    "mode_label",
    "mode_policy",
    "select_verification_mode",
    "mode_search_budget",
]
