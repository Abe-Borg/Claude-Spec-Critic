"""Centralized Anthropic API configuration for Spec Critic.

Phase 2 (API modernization): single place for model identifiers, per-phase
output-token caps, batch beta headers, web-search tool configuration, and
feature flags for prompt caching and Anthropic token-counting preflight.

All knobs are read from environment variables with sane defaults. Existing
constants in `verification_config.py` and `tokenizer.py` are re-exported here
so future model migration touches one file.

Feature flags (default-on unless noted):
    SPEC_CRITIC_PROMPT_CACHE                     — "0" disables prompt caching.
    SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT            — "0" disables Anthropic
                                                   count_tokens preflight.
    SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT      — "0" reverts to Opus default.
    SPEC_CRITIC_LOCAL_VERIFICATION_SKIP          — "0" disables local-skip.
    SPEC_CRITIC_PARALLEL_CROSS_CHECK             — "0" disables parallel cross-check.
    SPEC_CRITIC_REALTIME_FALLBACK_THRESHOLD      — int (default 5).
    SPEC_CRITIC_VERIFICATION_MAX_USES            — int override for default
                                                   web-search max_uses (used
                                                   when per-severity tiering
                                                   is disabled).
    SPEC_CRITIC_VERIFICATION_MODEL               — model id override for verification.
    SPEC_CRITIC_REVIEW_MODEL                     — model id override for review.
    SPEC_CRITIC_SYNTHESIS_MODEL                  — model id override for the
                                                   cross-discipline synthesis
                                                   pass (default Haiku 4.5).
    SPEC_CRITIC_TRIAGE_MODEL                     — model id override for
                                                   verification triage
                                                   (default Haiku 4.5).
    SPEC_CRITIC_HAIKU_TRIAGE                     — "1" enables Haiku-based
                                                   verification triage as an
                                                   augmentation of the
                                                   keyword classifier
                                                   (default off).
    SPEC_CRITIC_VERIFICATION_CACHE_PERSIST       — "0" disables on-disk
                                                   verification cache
                                                   (default on; database mode).
    SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS      — int; 0 means no expiry
                                                   (default 0).
    SPEC_CRITIC_CACHE_PATH                       — explicit cache path
                                                   (default ``~/.spec_critic
                                                   /verification_cache.json``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

# ---------------------------------------------------------------------------
# Model identifiers (centralized)
# ---------------------------------------------------------------------------

MODEL_OPUS_46 = "claude-opus-4-6"
MODEL_OPUS_47 = "claude-opus-4-7"
MODEL_SONNET_46 = "claude-sonnet-4-6"
MODEL_HAIKU_45 = "claude-haiku-4-5"

# Defaults. Phase 3 routes verification through Sonnet first and reserves
# Opus for escalation on CRITICAL/HIGH UNVERIFIED findings. Set
# SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT=0 to revert to Opus-everywhere.
REVIEW_MODEL_DEFAULT = os.environ.get("SPEC_CRITIC_REVIEW_MODEL", MODEL_OPUS_47)
CROSS_CHECK_MODEL_DEFAULT = os.environ.get("SPEC_CRITIC_CROSS_CHECK_MODEL", MODEL_OPUS_47)


def verification_sonnet_default_enabled() -> bool:
    """Whether Sonnet is the default verifier (Phase 3 routing).

    On by default. Verification is largely retrieval + comparison + verdict
    classification, which Sonnet handles well at materially lower cost. Opus
    remains the escalation target for CRITICAL/HIGH UNVERIFIED findings.
    Set SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT=0 to revert to the prior
    Opus-everywhere behavior.
    """
    return os.environ.get("SPEC_CRITIC_VERIFICATION_SONNET_DEFAULT", "1") != "0"


_VERIFICATION_MODEL_OVERRIDE = os.environ.get("SPEC_CRITIC_VERIFICATION_MODEL")
if _VERIFICATION_MODEL_OVERRIDE:
    VERIFICATION_MODEL_DEFAULT = _VERIFICATION_MODEL_OVERRIDE
elif verification_sonnet_default_enabled():
    VERIFICATION_MODEL_DEFAULT = MODEL_SONNET_46
else:
    VERIFICATION_MODEL_DEFAULT = MODEL_OPUS_47

# Model used when escalating a low-confidence/high-severity verification.
VERIFICATION_ESCALATION_MODEL = os.environ.get(
    "SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL", MODEL_OPUS_47
)

# Cross-discipline synthesis pass (cross_checker._run_cross_discipline_synthesis)
# correlates already-classified per-chunk findings — small input, small output,
# shallow reasoning. Haiku is appropriate; Opus is overkill.
SYNTHESIS_MODEL_DEFAULT = os.environ.get("SPEC_CRITIC_SYNTHESIS_MODEL", MODEL_HAIKU_45)

# Verification triage pre-pass (triage.classify_findings_with_haiku) decides
# whether a finding can be locally resolved or needs web verification. The
# task is shallow classification over short inputs; Haiku fits.
TRIAGE_MODEL_DEFAULT = os.environ.get("SPEC_CRITIC_TRIAGE_MODEL", MODEL_HAIKU_45)


# Convenience sets for output-cap dispatch.
OPUS_MODELS = frozenset({MODEL_OPUS_46, MODEL_OPUS_47})
HAIKU_MODELS = frozenset({MODEL_HAIKU_45})


# ---------------------------------------------------------------------------
# Output-token caps
# ---------------------------------------------------------------------------

# Hard ceilings imposed by the model.
MAX_OUTPUT_TOKENS_OPUS = 128_000
MAX_OUTPUT_TOKENS_SONNET = 64_000
MAX_OUTPUT_TOKENS_HAIKU = 64_000

# Extended-output batch beta. Required header to use 300k output in batch.
BATCH_OUTPUT_BETA = "output-300k-2026-03-24"
BATCH_MAX_OUTPUT_TOKENS = 300_000

# Per-phase dynamic caps. These are intentionally lower than the hard model
# ceilings so the app does not blanket-allocate the maximum on every call.
# The review cap is unified across real-time and batch so findings cannot
# diverge between modes on normal-size specs. (Anthropic bills by actual
# output, so the cap is a fail-fast guard, not a cost lever.) The extended
# 300k path is batch-only — the ``output-300k-2026-03-24`` beta header is
# not honored on streaming requests.
REVIEW_OUTPUT_CAP = 128_000              # baseline for both real-time and batch
REVIEW_OUTPUT_CAP_BATCH_EXTENDED = 300_000  # batch-only, with 300k beta header
CROSS_CHECK_OUTPUT_CAP = 96_000       # cross-check needs more than verify
# Verdicts are 1-2 sentences per the verifier system prompt; 16k is a
# fail-fast guard, not a billing knob (you pay only for actual output).
VERIFICATION_OUTPUT_CAP = 16_000
# Synthesis pass output is a handful of cross-division findings + a brief
# coordination summary; 32k leaves comfortable headroom while bounding
# runaway output if Haiku misbehaves.
SYNTHESIS_OUTPUT_CAP = 32_000
# Triage emits a small array of {index, classification, reason}; 8k is more
# than enough even for a 50-finding chunk.
HAIKU_TRIAGE_OUTPUT_CAP = 8_000

# Token threshold above which a review uses the larger batch cap.
LARGE_REVIEW_INPUT_THRESHOLD = 200_000


# Phase identifiers. Defined here (before the phase→budget registry) so
# the registry can reference them directly. ``thinking_config_for`` and
# ``apply_thinking_config`` further below also consume these.
PHASE_REVIEW = "review"
PHASE_BATCH_REVIEW = "batch_review"
PHASE_CROSS_CHECK = "cross_check"
PHASE_SYNTHESIS = "synthesis"
PHASE_VERIFICATION = "verification"
PHASE_VERIFICATION_RETRY = "verification_retry"
PHASE_VERIFICATION_CONTINUATION = "verification_continuation"
PHASE_TRIAGE = "triage"


def output_cap_for_model(model: str, *, requested: int) -> int:
    """Clamp ``requested`` to the model's hard output ceiling."""
    if model in OPUS_MODELS:
        ceiling = MAX_OUTPUT_TOKENS_OPUS
    elif model in HAIKU_MODELS:
        ceiling = MAX_OUTPUT_TOKENS_HAIKU
    else:
        ceiling = MAX_OUTPUT_TOKENS_SONNET
    return min(requested, ceiling)


# Chunk E directive 6: a single registry of per-phase output budgets so
# verification retry/continuation, synthesis, and triage all resolve through
# the same lookup. Each phase declares its desired cap; ``phase_output_cap``
# clamps that to the selected model's ceiling. The phase helpers below stay
# as thin wrappers so callers can keep their existing imports.
#
# Verification retry/continuation reuse the verification cap by default —
# the verdict envelope is unchanged across retries, so granting more output
# only invites the model to ramble. If a future investigation shows
# continuations need more headroom, this is the one place to tune it.
_PHASE_OUTPUT_BUDGET: dict[str, int] = {
    PHASE_REVIEW: REVIEW_OUTPUT_CAP,
    PHASE_BATCH_REVIEW: REVIEW_OUTPUT_CAP,
    PHASE_CROSS_CHECK: CROSS_CHECK_OUTPUT_CAP,
    PHASE_SYNTHESIS: SYNTHESIS_OUTPUT_CAP,
    PHASE_VERIFICATION: VERIFICATION_OUTPUT_CAP,
    PHASE_VERIFICATION_RETRY: VERIFICATION_OUTPUT_CAP,
    PHASE_VERIFICATION_CONTINUATION: VERIFICATION_OUTPUT_CAP,
    PHASE_TRIAGE: HAIKU_TRIAGE_OUTPUT_CAP,
}


def phase_output_cap(phase: str, *, model: str) -> int:
    """Return the centralized per-phase max_tokens budget for ``model``.

    Directive 6 of Chunk E: every phase resolves its output cap here so
    review, batch review, cross-check, synthesis, verification, verification
    retry, verification continuation, and triage all share one registry.
    Unknown phases fall back to the verification cap, the most conservative
    value in the registry — a future phase that forgets to register loses
    headroom instead of accidentally inheriting the 128k review cap.
    """
    requested = _PHASE_OUTPUT_BUDGET.get(phase, VERIFICATION_OUTPUT_CAP)
    return output_cap_for_model(model, requested=requested)


def synthesis_max_tokens(*, model: str = SYNTHESIS_MODEL_DEFAULT) -> int:
    return phase_output_cap(PHASE_SYNTHESIS, model=model)


def triage_max_tokens(*, model: str = TRIAGE_MODEL_DEFAULT) -> int:
    return phase_output_cap(PHASE_TRIAGE, model=model)


def review_max_tokens(*, batch: bool = False, model: str = REVIEW_MODEL_DEFAULT, allow_extended_output: bool = False) -> int:
    """Return a per-call max_tokens for a review request.

    Real-time and batch share the same baseline so findings cannot diverge
    between modes on normal-size specs. ``allow_extended_output`` selects
    the 300k batch-only path; the beta header is checked at the call site
    by :func:`assert_extended_output_allowed`.
    """
    if batch and allow_extended_output:
        return min(BATCH_MAX_OUTPUT_TOKENS, REVIEW_OUTPUT_CAP_BATCH_EXTENDED)
    phase = PHASE_BATCH_REVIEW if batch else PHASE_REVIEW
    return phase_output_cap(phase, model=model)


def cross_check_max_tokens(*, model: str = CROSS_CHECK_MODEL_DEFAULT) -> int:
    return phase_output_cap(PHASE_CROSS_CHECK, model=model)


def verification_max_tokens(*, model: str = VERIFICATION_MODEL_DEFAULT, phase: str = PHASE_VERIFICATION) -> int:
    """Return a per-call max_tokens for a verification request.

    ``phase`` defaults to ``PHASE_VERIFICATION``; pass
    ``PHASE_VERIFICATION_RETRY`` or ``PHASE_VERIFICATION_CONTINUATION`` to
    pick up retry-specific or continuation-specific budgets from the central
    registry. Today all three resolve to the same cap; the parameter exists
    so a future tuning pass touches one place.
    """
    return phase_output_cap(phase, model=model)


def assert_extended_output_allowed(*, max_tokens: int, betas: Iterable[str] | None) -> None:
    """Guard against 300k output without the required beta header.

    The Anthropic API rejects 300k output when the extended-output beta is
    not set, but the failure surfaces deep in the request lifecycle. Plan
    Sprint 2 item 8: fail fast at the call site instead.
    """
    if max_tokens <= MAX_OUTPUT_TOKENS_OPUS:
        return
    beta_set = set(betas or ())
    if BATCH_OUTPUT_BETA not in beta_set:
        raise ValueError(
            f"Requested max_tokens={max_tokens:,} requires beta header "
            f"'{BATCH_OUTPUT_BETA}'. Refusing to submit without it."
        )


# ---------------------------------------------------------------------------
# Model capability policy (Chunk B)
# ---------------------------------------------------------------------------
#
# Whitelist-style registry of per-model capabilities. The Anthropic API
# rejects requests that include feature parameters the selected model does
# not support — most notably ``thinking`` against Haiku 4.5, which produces
# an API error. Prior to this policy, every request path hard-coded
# ``thinking={"type": "adaptive"}``, which blew up the cross-discipline
# synthesis path the moment its default model moved to Haiku.
#
# To add a new model: register it in ``_MODEL_CAPABILITIES``. Unknown model
# IDs fall through to ``_DEFAULT_CAPABILITIES``, which disables every
# capability flag — intentional. Stripping a feature from a future model is
# strictly safer than sending an invalid request that fails deep in the
# request lifecycle.


@dataclass(frozen=True)
class ModelCapabilities:
    """Per-model feature support. Drives request-shape decisions."""

    supports_adaptive_thinking: bool
    max_output_tokens: int
    supports_extended_output_beta: bool  # 300k batch-only beta header
    context_window: int


_MODEL_CAPABILITIES: dict[str, ModelCapabilities] = {
    MODEL_OPUS_46: ModelCapabilities(
        supports_adaptive_thinking=True,
        max_output_tokens=MAX_OUTPUT_TOKENS_OPUS,
        supports_extended_output_beta=True,
        context_window=1_000_000,
    ),
    MODEL_OPUS_47: ModelCapabilities(
        supports_adaptive_thinking=True,
        max_output_tokens=MAX_OUTPUT_TOKENS_OPUS,
        supports_extended_output_beta=True,
        context_window=1_000_000,
    ),
    MODEL_SONNET_46: ModelCapabilities(
        supports_adaptive_thinking=True,
        max_output_tokens=MAX_OUTPUT_TOKENS_SONNET,
        supports_extended_output_beta=False,
        context_window=1_000_000,
    ),
    MODEL_HAIKU_45: ModelCapabilities(
        # Anthropic models overview lists Haiku 4.5 without adaptive
        # thinking support; sending ``thinking`` to it returns an API error.
        supports_adaptive_thinking=False,
        max_output_tokens=MAX_OUTPUT_TOKENS_HAIKU,
        supports_extended_output_beta=False,
        context_window=200_000,
    ),
}


# Unknown models: every capability flag defaults to False so we never
# construct an invalid request payload. Output cap defaults to the Sonnet
# ceiling, the most conservative of the supported models that still leaves
# room for a meaningful response.
_DEFAULT_CAPABILITIES = ModelCapabilities(
    supports_adaptive_thinking=False,
    max_output_tokens=MAX_OUTPUT_TOKENS_SONNET,
    supports_extended_output_beta=False,
    context_window=200_000,
)


def model_capabilities(model: str) -> ModelCapabilities:
    """Return the capability record for ``model`` (or safe defaults)."""
    return _MODEL_CAPABILITIES.get(model, _DEFAULT_CAPABILITIES)


def model_supports_adaptive_thinking(model: str) -> bool:
    """Whether ``model`` accepts the ``thinking`` request parameter."""
    return model_capabilities(model).supports_adaptive_thinking


# Phase identifiers (declared above so the phase→budget registry can use
# them) gate per-phase request decisions. ``_PHASES_NO_THINKING`` is the
# extension point for phases that should never request thinking regardless
# of model capability — currently only the Haiku triage classifier, which
# is a shallow batch-classification pass.
_PHASES_NO_THINKING: frozenset[str] = frozenset({PHASE_TRIAGE})


def thinking_config_for(*, model: str, phase: str) -> dict | None:
    """Return the ``thinking`` request parameter for ``(model, phase)``.

    Returns ``None`` when the parameter should be omitted entirely —
    either the phase opts out, or the model does not support adaptive
    thinking. Callers should branch on ``is None``; the Anthropic API
    rejects ``thinking=null``.
    """
    if phase in _PHASES_NO_THINKING:
        return None
    if not model_supports_adaptive_thinking(model):
        return None
    return {"type": "adaptive"}


def apply_thinking_config(kwargs: dict, *, model: str, phase: str) -> dict:
    """Insert the ``thinking`` key into ``kwargs`` only when applicable.

    Mutates and returns ``kwargs`` for fluent use. The key is omitted
    entirely (not set to ``None``) when thinking is not applicable, because
    the Anthropic API rejects ``thinking=null``.
    """
    config = thinking_config_for(model=model, phase=phase)
    if config is not None:
        kwargs["thinking"] = config
    return kwargs


# ---------------------------------------------------------------------------
# Prompt caching (Chunk J: centralized phase-aware policy)
# ---------------------------------------------------------------------------
#
# Background: prior to Chunk J every call site decided independently to wrap
# its system prompt and tool list through ``system_prompt_with_cache`` /
# ``tools_with_cache``. The helpers themselves had a single global on/off and
# a single TTL, so triage (375-token system prompt) and synthesis (425-token
# system prompt, called once per run) paid the cache-write overhead even
# though both prompts are below the Anthropic 1024-token cache minimum and
# could never produce a hit.
#
# Chunk J directives 2–5 ask for the policy to be centralized and phase-aware
# instead. Each phase declares whether its system prompt and tool list are
# stable / large / repeated enough to benefit from caching. The defaults
# below preserve current behavior for the high-value phases (review, batch
# review, cross-check, verification + retry/continuation) and disable
# caching for the two phases the directives explicitly call out as
# inappropriate (synthesis = one-off, triage = small prompt).
#
# Operators can override individual phases via ``SPEC_CRITIC_CACHE_DISABLE``
# without rebuilding the helpers — see :func:`_phase_disabled_via_env`.


def prompt_caching_enabled() -> bool:
    """Whether to attach cache_control breakpoints to stable prompt prefixes."""
    return os.environ.get("SPEC_CRITIC_PROMPT_CACHE", "1") != "0"


@dataclass(frozen=True)
class CachePolicy:
    """Per-phase cache policy.

    ``cache_system`` and ``cache_tools`` independently control whether the
    system prompt and the trailing tool block carry ``cache_control``
    breakpoints. ``ttl`` is the desired cache TTL (``"5m"`` or ``"1h"``);
    ``None`` defers to the global ``SPEC_CRITIC_PROMPT_CACHE_TTL`` setting,
    which preserves the pre-Chunk-J default (``"1h"``).
    """

    cache_system: bool
    cache_tools: bool
    ttl: str | None = None

    @property
    def caches_anything(self) -> bool:
        return self.cache_system or self.cache_tools


# Default per-phase policies. The directive-driven rationale for each entry
# is documented inline so future tuning has the reasoning in one place.
_DEFAULT_PHASE_CACHE_POLICY = CachePolicy(cache_system=True, cache_tools=True, ttl=None)

_PHASE_CACHE_POLICY: dict[str, CachePolicy] = {
    # Real-time review: same review system prompt is reused across every
    # spec in a multi-file selection, and the structured tool schema is
    # stable. Worth caching at the 1h TTL because a typical project review
    # touches 5–20 specs over several minutes.
    PHASE_REVIEW: CachePolicy(cache_system=True, cache_tools=True, ttl=None),
    # Batch review: dozens of identical system+tools prefixes go out in one
    # shot. Anthropic explicitly documents that batch cache hits are
    # best-effort but stack with batch pricing — directive 2 says "Main
    # batch review likely yes."
    PHASE_BATCH_REVIEW: CachePolicy(cache_system=True, cache_tools=True, ttl=None),
    # Cross-check: only one or two calls per run when the input fits, but
    # the chunked path can fire 5+ calls with the same system prompt.
    PHASE_CROSS_CHECK: CachePolicy(cache_system=True, cache_tools=True, ttl=None),
    # Synthesis: single one-off call per run, ~425-token system prompt.
    # Below the 1024-token cache minimum for Sonnet/Opus and the 2048-
    # token minimum for Haiku, so a cache write would be paid for nothing.
    # Directive 5: "Avoid caching tiny, one-off prompts."
    PHASE_SYNTHESIS: CachePolicy(cache_system=False, cache_tools=False, ttl=None),
    # Verification: the system prompt and tool list are large and
    # genuinely reused across waves. Directive 2: "Verification waves
    # likely yes if prefixes/tools are reused."
    PHASE_VERIFICATION: CachePolicy(cache_system=True, cache_tools=True, ttl=None),
    PHASE_VERIFICATION_RETRY: CachePolicy(cache_system=True, cache_tools=True, ttl=None),
    PHASE_VERIFICATION_CONTINUATION: CachePolicy(cache_system=True, cache_tools=True, ttl=None),
    # Triage: ~375-token system prompt called in batches of up to 20.
    # Below the cache minimum for Haiku (2048 tokens) so even repeated
    # calls cannot hit. Directive 2: "Short triage likely no unless
    # measurements justify it."
    PHASE_TRIAGE: CachePolicy(cache_system=False, cache_tools=False, ttl=None),
}


def _phase_disabled_via_env(phase: str) -> bool:
    """Whether ``SPEC_CRITIC_CACHE_DISABLE`` opts ``phase`` out of caching.

    ``SPEC_CRITIC_CACHE_DISABLE`` is a comma-separated list of phase names.
    Lets operators turn off caching for individual phases without flipping
    the global ``SPEC_CRITIC_PROMPT_CACHE`` switch — useful when a particular
    phase is misbehaving but the rest of the pipeline still benefits from
    caching. Whitespace and case are ignored.
    """
    raw = os.environ.get("SPEC_CRITIC_CACHE_DISABLE", "").strip()
    if not raw:
        return False
    disabled = {p.strip().lower() for p in raw.split(",") if p.strip()}
    return phase.lower() in disabled


def cache_policy_for(phase: str | None) -> CachePolicy:
    """Return the per-phase :class:`CachePolicy`.

    Unknown phases fall back to the conservative default (cache both system
    prompt and tools at the global TTL). ``phase=None`` also returns the
    default — used by callers that have not yet been migrated to phase-
    aware caching.
    """
    if phase is not None and _phase_disabled_via_env(phase):
        return CachePolicy(cache_system=False, cache_tools=False, ttl=None)
    if phase is None:
        return _DEFAULT_PHASE_CACHE_POLICY
    return _PHASE_CACHE_POLICY.get(phase, _DEFAULT_PHASE_CACHE_POLICY)


def _cache_control_block(*, ttl_override: str | None = None) -> dict:
    """Return the standard cache_control block.

    Spec Critic batch + verification waves run for 30 minutes to several hours,
    well beyond the 5-minute default ephemeral cache TTL. The 1-hour TTL
    costs 2x the cache write but typically pays back inside the second wave
    of a batch verification cycle, where the same system prompt is sent
    hundreds of times. Set ``SPEC_CRITIC_PROMPT_CACHE_TTL=5m`` to revert.

    ``ttl_override`` lets the per-phase policy pick a different TTL than the
    global default (currently unused — every phase inherits the global TTL —
    but the lever exists for future tuning).
    """
    ttl = (ttl_override or os.environ.get("SPEC_CRITIC_PROMPT_CACHE_TTL", "1h")).strip().lower()
    if ttl == "5m":
        return {"type": "ephemeral"}
    return {"type": "ephemeral", "ttl": "1h"}


def system_prompt_with_cache(prompt: str, *, phase: str | None = None):
    """Return a system payload with a cache breakpoint when enabled.

    When caching is enabled and the phase policy permits, returns a
    one-element list of TextBlockParam dicts with cache_control set. When
    disabled (globally, by phase policy, or by ``SPEC_CRITIC_CACHE_DISABLE``),
    returns the original string so the API call shape is unchanged.

    ``phase`` selects the per-phase policy (Chunk J directive 3). Callers
    that do not yet pass ``phase`` get the legacy default behavior, which
    caches when the global flag is on. Migrating a call site is purely
    additive — supplying ``phase`` lets the central registry decide whether
    caching actually pays off for that phase.

    Per the Anthropic prompt-caching docs, including the same cache_control
    blocks in every request in a batch lets later items hit the cache
    created by earlier items.
    """
    if not prompt_caching_enabled():
        return prompt
    policy = cache_policy_for(phase)
    if not policy.cache_system:
        return prompt
    return [
        {
            "type": "text",
            "text": prompt,
            "cache_control": _cache_control_block(ttl_override=policy.ttl),
        }
    ]


def tools_with_cache(tools: list[dict], *, phase: str | None = None) -> list[dict]:
    """Attach a cache breakpoint to the last tool definition.

    Tool schemas are stable across verification calls. Caching the trailing
    tool block lets the rest of the request (system prompt + tool defs)
    share one cache prefix. The system prompt has its own breakpoint via
    :func:`system_prompt_with_cache`, so changing only a tool definition
    invalidates only the tools-level cache entry.

    ``phase`` selects the per-phase policy. When the policy disables tool
    caching for the phase (e.g. triage / synthesis where the prompt is
    below the cache minimum), the tool list is returned unchanged.
    """
    if not prompt_caching_enabled() or not tools:
        return tools
    policy = cache_policy_for(phase)
    if not policy.cache_tools:
        return tools
    last = dict(tools[-1])
    last["cache_control"] = _cache_control_block(ttl_override=policy.ttl)
    return [*tools[:-1], last]


# ---------------------------------------------------------------------------
# Service tier (priority capacity)
# ---------------------------------------------------------------------------


def batch_service_tier() -> str | None:
    """Return the ``service_tier`` parameter to set on batch request params.

    ``auto`` (default) opts batch requests into priority capacity when
    available, falling back to standard. Set ``SPEC_CRITIC_SERVICE_TIER``
    to ``standard_only`` to pin to standard, or to an empty string to omit
    the field entirely (some SDK versions or accounts may not accept it).
    """
    tier = os.environ.get("SPEC_CRITIC_SERVICE_TIER", "auto").strip()
    return tier or None


# ---------------------------------------------------------------------------
# Anthropic token-counting preflight (opt-in)
# ---------------------------------------------------------------------------

def token_count_preflight_enabled() -> bool:
    """Whether to call Anthropic's count_tokens endpoint before submission.

    On by default. The GUI also runs an exact count for the largest spec
    when the file list changes (Phase 2.3); the pipeline call here is the
    moment-of-truth guard before a real submission. Set
    SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT=0 to disable.
    """
    return os.environ.get("SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT", "1") != "0"


# ---------------------------------------------------------------------------
# Web-search tool configuration
# ---------------------------------------------------------------------------

# Source-quality blocklist for web_search_20260209. Audit notes (section 6.8)
# call out that mixing allowed_domains and blocked_domains is not supported,
# so we keep blocked-only here. California priority sources are documented in
# the verifier system prompt rather than encoded as an allow-list.
_WEB_SEARCH_BLOCKED_DOMAINS = [
    "reddit.com", "quora.com", "medium.com",
    "chatgpt.com", "perplexity.ai", "openai.com", "gemini.google.com",
    "claude.ai", "you.com", "phind.com", "copilot.microsoft.com",
    "poe.com", "character.ai", "jasper.ai", "writesonic.com",
    "stackexchange.com", "stackoverflow.com",
    "answers.yahoo.com", "fixya.com",
    "diychatroom.com", "forums.jlconline.com", "hvac-talk.com",
    "inspectionnews.net", "inspectorsforum.com", "contractortalk.com",
    "doityourself.com", "homeadvisor.com", "thumbtack.com", "angi.com",
    "ehow.com", "wikihow.com", "about.com", "thespruce.com", "bobvila.com",
    "familyhandyman.com", "hunker.com", "sapling.com", "reference.com",
    "leaf.tv", "sciencing.com", "bizfluent.com", "pocketsense.com",
    "facebook.com", "twitter.com", "x.com", "instagram.com", "tiktok.com",
    "linkedin.com", "pinterest.com", "youtube.com", "threads.net",
    "wikipedia.org", "britannica.com", "simple.wikipedia.org",
]

# Default web-search budget when severity tiering is disabled or the severity
# is not recognized. Lowered from prior 10. Plan section 6.8 recommends
# reducing default max_uses for simple verification and escalating only when
# needed.
DEFAULT_VERIFICATION_MAX_USES = int(
    os.environ.get("SPEC_CRITIC_VERIFICATION_MAX_USES", "5")
)

# Per-severity search budgets. High-stakes claims get more rope; editorial
# gripes get less. Applied identically to real-time and batch verification
# paths so the budget shape doesn't depend on which mode you ran in.
_SEVERITY_MAX_USES: dict[str, int] = {
    "CRITICAL": 7,
    "HIGH": 7,
    "MEDIUM": 5,
    "GRIPES": 3,
}


def web_search_max_uses_for_severity(severity: str | None) -> int:
    """Return the per-severity web_search budget.

    Falls back to ``DEFAULT_VERIFICATION_MAX_USES`` for unknown severities so
    a misclassified finding still gets a reasonable budget.
    """
    sev = (severity or "").strip().upper()
    return _SEVERITY_MAX_USES.get(sev, DEFAULT_VERIFICATION_MAX_USES)


def build_web_search_tool(*, max_uses: int = DEFAULT_VERIFICATION_MAX_USES) -> dict:
    return {
        "type": "web_search_20260209",
        "name": "web_search",
        "blocked_domains": list(_WEB_SEARCH_BLOCKED_DOMAINS),
        "max_uses": max_uses,
        "user_location": {
            "type": "approximate",
            "country": "US",
            "region": "California",
        },
    }


def web_search_tool_for_severity(severity: str | None) -> dict:
    """Build a web_search tool dict with a per-severity ``max_uses`` budget."""
    return build_web_search_tool(
        max_uses=web_search_max_uses_for_severity(severity),
    )


# Default web-search tool used when no per-call override is needed (preserved
# for backward compatibility with any caller that imports the constant).
WEB_SEARCH_TOOL = build_web_search_tool()


# ---------------------------------------------------------------------------
# Cache-token usage extraction (for diagnostics)
# ---------------------------------------------------------------------------

def extract_cache_usage(usage) -> dict[str, int]:
    """Pull cache-related fields off an Anthropic usage object.

    Returns a dict with keys ``cache_creation_input_tokens`` and
    ``cache_read_input_tokens`` (zero when absent). The Anthropic SDK
    exposes these on ``Message.usage`` when prompt caching is in effect.
    """
    if usage is None:
        return {"cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    return {
        "cache_creation_input_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        "cache_read_input_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
    }
