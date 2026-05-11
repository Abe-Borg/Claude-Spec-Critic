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
# Prompt caching
# ---------------------------------------------------------------------------

def prompt_caching_enabled() -> bool:
    """Whether to attach cache_control breakpoints to stable prompt prefixes."""
    return os.environ.get("SPEC_CRITIC_PROMPT_CACHE", "1") != "0"


def _cache_control_block() -> dict:
    """Return the standard cache_control block.

    Spec Critic batch + verification waves run for 30 minutes to several hours,
    well beyond the 5-minute default ephemeral cache TTL. The 1-hour TTL
    costs 2x the cache write but typically pays back inside the second wave
    of a batch verification cycle, where the same system prompt is sent
    hundreds of times. Set ``SPEC_CRITIC_PROMPT_CACHE_TTL=5m`` to revert.
    """
    ttl = os.environ.get("SPEC_CRITIC_PROMPT_CACHE_TTL", "1h").strip().lower()
    if ttl == "5m":
        return {"type": "ephemeral"}
    return {"type": "ephemeral", "ttl": "1h"}


def system_prompt_with_cache(prompt: str):
    """Return a system payload with a cache breakpoint when enabled.

    When caching is enabled, returns a one-element list of TextBlockParam
    dicts with cache_control set. When disabled, returns the original string
    so the API call shape is unchanged.

    This is the primary mechanism for caching identical review/cross-check
    /verification system prompts across many requests in a batch. Per the
    Anthropic prompt-caching docs, including the same cache_control blocks
    in every request in a batch lets later items hit the cache created by
    earlier items.
    """
    if not prompt_caching_enabled():
        return prompt
    return [
        {
            "type": "text",
            "text": prompt,
            "cache_control": _cache_control_block(),
        }
    ]


def tools_with_cache(tools: list[dict]) -> list[dict]:
    """Attach a cache breakpoint to the last tool definition.

    Tool schemas are stable across verification calls. Caching the trailing
    tool block lets the rest of the request (system prompt + tool defs)
    share one cache prefix. The system prompt has its own breakpoint via
    :func:`system_prompt_with_cache`, so changing only a tool definition
    invalidates only the tools-level cache entry.
    """
    if not prompt_caching_enabled() or not tools:
        return tools
    last = dict(tools[-1])
    last["cache_control"] = _cache_control_block()
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
