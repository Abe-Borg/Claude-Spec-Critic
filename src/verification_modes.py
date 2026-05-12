"""Verification modes and model routing.

A finding's verification mode bundles the ``(model, thinking_enabled,
web_search_enabled, allows_escalation)`` decisions into one record.
The search budget itself is severity-based and lives in
:mod:`verification_profiles`; modes pick whether to attach the web
search tool at all, not how much budget it gets.

Public surface:

- :class:`VerificationMode` — the four-value enum.
- :class:`ModePolicy` — frozen dataclass holding the per-mode policy
  knobs.
- :func:`select_verification_mode` — pure-function router from a
  ``Finding`` (+ context) to a :class:`VerificationMode`.
- :func:`mode_policy` — table lookup.
- :func:`mode_label` — pretty label for reports / diagnostics.

Design rules:

- **Deterministic routing.** The router is a pure function over the
  finding text + a few booleans (escalated, classifier verdict). No
  LLM is consulted to pick a mode — the goal is for the routing
  decision to be reproducible and inspectable.
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
        Identity of the mode this policy describes.
    model:
        Default Anthropic model id for this mode. The verifier may
        still override with an explicit ``model=`` keyword (operator
        overrides, escalation paths, tests), but the default flows
        from here.
    thinking_enabled:
        Whether the verifier should request ``thinking`` on this
        call. ``False`` for LOCAL_SKIP (which makes no remote call
        anyway) and STRICT_STRUCTURED (cheap, narrow).
    web_search_enabled:
        Whether the request should attach the web_search tool. Only
        ``False`` for LOCAL_SKIP; every other mode uses the full
        severity-based budget from :func:`verification_profiles.profile_max_uses`.
    allows_escalation:
        Whether a failed verification in this mode is eligible to
        escalate. ``False`` for LOCAL_SKIP (terminal), STRICT_STRUCTURED
        (low-stakes), and DEEP_REASONING (already at the top of the
        ladder). Only STANDARD_REASONING escalates.
    """

    mode: VerificationMode
    model: str
    thinking_enabled: bool
    web_search_enabled: bool
    allows_escalation: bool


def _default_initial_model() -> str:
    """The model used by STANDARD_REASONING's initial pass."""
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
            web_search_enabled=False,
            allows_escalation=False,
        )
    if mode is VerificationMode.STRICT_STRUCTURED:
        # Sonnet, no thinking — the cheap / narrow path. STRICT_STRUCTURED
        # stays on the cheaper model even when the operator overrides the
        # default verifier to Opus; the whole point of the mode is "use a
        # cheaper path for findings that do not need deep reasoning."
        return ModePolicy(
            mode=mode,
            model=MODEL_SONNET_46,
            thinking_enabled=False,
            web_search_enabled=True,
            allows_escalation=False,
        )
    if mode is VerificationMode.DEEP_REASONING:
        return ModePolicy(
            mode=mode,
            model=_deep_reasoning_model(),
            thinking_enabled=True,
            web_search_enabled=True,
            allows_escalation=False,
        )
    # STANDARD_REASONING — the default.
    return ModePolicy(
        mode=VerificationMode.STANDARD_REASONING,
        model=_default_initial_model(),
        thinking_enabled=True,
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


__all__ = [
    "VerificationMode",
    "ModePolicy",
    "mode_label",
    "mode_policy",
    "select_verification_mode",
]
