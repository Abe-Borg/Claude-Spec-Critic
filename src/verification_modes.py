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
    return ModePolicy(
        mode=VerificationMode.STANDARD_REASONING,
        model=_default_initial_model(),
        thinking_enabled=True,
        web_search_enabled=True,
        allows_escalation=True,
    )




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
    if cached_mode is not None:
        if isinstance(cached_mode, VerificationMode):
            return cached_mode
        try:
            return VerificationMode(cached_mode)
        except (TypeError, ValueError):
            pass

    if local_skip:
        return VerificationMode.LOCAL_SKIP

    if escalated:
        return VerificationMode.DEEP_REASONING

    if finding is None:
        return VerificationMode.STANDARD_REASONING

    severity = (getattr(finding, "severity", None) or "").strip().upper()
    profile = classify_finding_profile(finding)

    if severity == "CRITICAL" and profile is VerificationProfile.CALIFORNIA_AHJ:
        return VerificationMode.DEEP_REASONING

    if severity == "GRIPES":
        return VerificationMode.STRICT_STRUCTURED

    if profile is VerificationProfile.INTERNAL_COORDINATION:
        return VerificationMode.STRICT_STRUCTURED

    return VerificationMode.STANDARD_REASONING


__all__ = [
    "VerificationMode",
    "ModePolicy",
    "mode_label",
    "mode_policy",
    "select_verification_mode",
]
