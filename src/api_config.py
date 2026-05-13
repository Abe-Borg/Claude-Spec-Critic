"""Centralized Anthropic API configuration for Spec Critic.

Single place for model identifiers, per-phase output-token caps, batch
beta headers, web-search tool configuration, and request-shape policy
(prompt caching, adaptive thinking, effort).

Model identifiers may be overridden via env vars:
    SPEC_CRITIC_REVIEW_MODEL                — review (default Opus 4.7).
    SPEC_CRITIC_VERIFICATION_MODEL          — verification initial pass
                                              (default Sonnet 4.6).
    SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL — escalation (default Opus 4.7).
    SPEC_CRITIC_TRIAGE_MODEL                — verification triage
                                              (default Haiku 4.5).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

# ---------------------------------------------------------------------------
# Model identifiers (centralized)
# ---------------------------------------------------------------------------

MODEL_OPUS_47 = "claude-opus-4-7"
MODEL_SONNET_46 = "claude-sonnet-4-6"
MODEL_HAIKU_45 = "claude-haiku-4-5"

# Verification routes through Sonnet first and reserves Opus for escalation
# on CRITICAL/HIGH UNVERIFIED findings.
REVIEW_MODEL_DEFAULT = os.environ.get("SPEC_CRITIC_REVIEW_MODEL", MODEL_OPUS_47)
CROSS_CHECK_MODEL_DEFAULT = MODEL_SONNET_46
VERIFICATION_MODEL_DEFAULT = os.environ.get(
    "SPEC_CRITIC_VERIFICATION_MODEL", MODEL_SONNET_46
)

# Model used when escalating a low-confidence/high-severity verification.
VERIFICATION_ESCALATION_MODEL = os.environ.get(
    "SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL", MODEL_OPUS_47
)

# Verification triage pre-pass (triage.classify_findings_with_haiku) decides
# whether a finding can be locally resolved or needs web verification. The
# task is shallow classification over short inputs; Haiku fits.
TRIAGE_MODEL_DEFAULT = os.environ.get("SPEC_CRITIC_TRIAGE_MODEL", MODEL_HAIKU_45)


# Convenience sets for output-cap dispatch.
OPUS_MODELS = frozenset({MODEL_OPUS_47})
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


# Single registry of per-phase output budgets so verification
# retry/continuation and triage all resolve through the same lookup. Each
# phase declares its desired cap; ``phase_output_cap`` clamps that to the
# selected model's ceiling. The phase helpers below stay as thin wrappers
# so callers can keep their existing imports.
#
# Verification retry/continuation reuse the verification cap by default —
# the verdict envelope is unchanged across retries, so granting more output
# only invites the model to ramble. If a future investigation shows
# continuations need more headroom, this is the one place to tune it.
_PHASE_OUTPUT_BUDGET: dict[str, int] = {
    PHASE_REVIEW: REVIEW_OUTPUT_CAP,
    PHASE_BATCH_REVIEW: REVIEW_OUTPUT_CAP,
    PHASE_CROSS_CHECK: CROSS_CHECK_OUTPUT_CAP,
    PHASE_VERIFICATION: VERIFICATION_OUTPUT_CAP,
    PHASE_VERIFICATION_RETRY: VERIFICATION_OUTPUT_CAP,
    PHASE_VERIFICATION_CONTINUATION: VERIFICATION_OUTPUT_CAP,
    PHASE_TRIAGE: HAIKU_TRIAGE_OUTPUT_CAP,
}


def phase_output_cap(phase: str, *, model: str) -> int:
    """Return the centralized per-phase max_tokens budget for ``model``.

    Every phase resolves its output cap here so review, batch review,
    cross-check, verification, verification retry, verification continuation,
    and triage all share one registry. Unknown phases fall back to the
    verification cap, the most conservative value in the registry — a future
    phase that forgets to register loses headroom instead of accidentally
    inheriting the 128k review cap.
    """
    requested = _PHASE_OUTPUT_BUDGET.get(phase, VERIFICATION_OUTPUT_CAP)
    return output_cap_for_model(model, requested=requested)


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
# an API error.
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
    # Chunk D1.2: whether the model accepts ``output_config.effort``. The
    # parameter controls token eagerness and tool-call behavior. Sending
    # it to an unsupported model returns an API error, so the policy in
    # :func:`effort_config_for` must consult this flag before attaching
    # the field. Default ``False`` so unknown models silently omit it.
    supports_effort: bool = False


_MODEL_CAPABILITIES: dict[str, ModelCapabilities] = {
    MODEL_OPUS_47: ModelCapabilities(
        supports_adaptive_thinking=True,
        max_output_tokens=MAX_OUTPUT_TOKENS_OPUS,
        supports_extended_output_beta=True,
        context_window=1_000_000,
        supports_effort=True,
    ),
    MODEL_SONNET_46: ModelCapabilities(
        supports_adaptive_thinking=True,
        max_output_tokens=MAX_OUTPUT_TOKENS_SONNET,
        # Chunk 1: Sonnet 4.6 supports the ``output-300k-2026-03-24`` beta
        # on Message Batches. The prior ``False`` value predated that
        # capability rollout and forced the batch path to gate extended
        # output by Opus-only family membership.
        supports_extended_output_beta=True,
        context_window=1_000_000,
        supports_effort=True,
    ),
    MODEL_HAIKU_45: ModelCapabilities(
        # Anthropic models overview lists Haiku 4.5 without adaptive
        # thinking support; sending ``thinking`` to it returns an API error.
        supports_adaptive_thinking=False,
        max_output_tokens=MAX_OUTPUT_TOKENS_HAIKU,
        supports_extended_output_beta=False,
        context_window=200_000,
        # The Anthropic effort docs list Haiku 4.5 without effort support.
        # Omit ``output_config.effort`` for Haiku to keep request shapes
        # safe across model swaps (e.g. triage).
        supports_effort=False,
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
    supports_effort=False,
)


def model_capabilities(model: str) -> ModelCapabilities:
    """Return the capability record for ``model`` (or safe defaults)."""
    return _MODEL_CAPABILITIES.get(model, _DEFAULT_CAPABILITIES)


def model_supports_adaptive_thinking(model: str) -> bool:
    """Whether ``model`` accepts the ``thinking`` request parameter."""
    return model_capabilities(model).supports_adaptive_thinking


def model_supports_effort(model: str) -> bool:
    """Whether ``model`` accepts the ``output_config.effort`` parameter.

    Chunk D1.2: callers MUST check this before attaching
    ``output_config={"effort": ...}`` to a request. Unsupported models
    (Haiku 4.5, unknown / future models) silently omit the field — the
    field is opt-in per model, so omitting it is always safe.
    """
    return model_capabilities(model).supports_effort


def model_supports_extended_output_beta(model: str) -> bool:
    """Whether ``model`` is eligible for the 300k batch-output beta.

    Chunk 1: the extended-output decision must read from the capability
    registry rather than testing ``model in OPUS_MODELS``. Sonnet 4.6
    supports the ``output-300k-2026-03-24`` beta on Message Batches,
    which the family-style check incorrectly excluded.
    """
    return model_capabilities(model).supports_extended_output_beta


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
# Output-config effort policy (Chunk D1.2)
# ---------------------------------------------------------------------------
#
# The Anthropic API accepts an ``output_config.effort`` parameter on
# supported models. The value tunes how eagerly the model produces tokens
# and how aggressively it pursues tool calls. The four documented levels
# are ``low`` / ``medium`` / ``high`` / ``xhigh`` (plus ``max``); we don't
# use ``max`` because it overshoots the verification verdict envelope.
#
# Effort is a request-policy decision, not a prompt one. Centralizing it
# here keeps every request site (review / batch review / cross-check /
# verification / retry / continuation) reaching for the same lever via
# :func:`apply_effort_config`. Unsupported models silently omit the
# parameter via :func:`model_supports_effort`.
#
# Default policy:
#
# - Sonnet verification (PHASE_VERIFICATION{,_RETRY,_CONTINUATION}): medium.
# - Opus verification (i.e. escalation): high.
# - Opus/Sonnet deep review (PHASE_REVIEW, PHASE_BATCH_REVIEW,
#   PHASE_CROSS_CHECK): high.
# - Triage (Haiku): omit (Haiku does not support effort).
# - Unknown model: omit.

EFFORT_LOW = "low"
EFFORT_MEDIUM = "medium"
EFFORT_HIGH = "high"
EFFORT_XHIGH = "xhigh"

# Phases whose request paths route through ``output_config.effort``. Triage
# is intentionally omitted — it defaults to Haiku which does not support
# effort, and the workload is a small classification pass that does not
# benefit from elevated effort.
_PHASE_DEFAULT_EFFORT: dict[str, str] = {
    PHASE_REVIEW: EFFORT_HIGH,
    PHASE_BATCH_REVIEW: EFFORT_HIGH,
    PHASE_CROSS_CHECK: EFFORT_HIGH,
    PHASE_VERIFICATION: EFFORT_MEDIUM,
    PHASE_VERIFICATION_RETRY: EFFORT_MEDIUM,
    PHASE_VERIFICATION_CONTINUATION: EFFORT_MEDIUM,
}

# Verification phases get the model-aware bump: Opus on verification is
# always the escalation tier, so the policy lifts effort to ``high``.
_VERIFICATION_PHASES: frozenset[str] = frozenset(
    {
        PHASE_VERIFICATION,
        PHASE_VERIFICATION_RETRY,
        PHASE_VERIFICATION_CONTINUATION,
    }
)


def effort_config_for(*, model: str, phase: str) -> dict | None:
    """Return the ``output_config`` dict for ``(model, phase)``, or ``None``.

    Returns ``None`` (i.e. "omit the field") when:

    - the model does not support effort (Haiku, unknown / future models),
    - the phase has no registered default (triage — defaults to Haiku,
      which already short-circuits above).

    Otherwise returns ``{"effort": <level>}`` where the level is ``high``
    for Opus on a verification phase (the escalation tier) or the phase
    default from :data:`_PHASE_DEFAULT_EFFORT`.
    """
    if not model_supports_effort(model):
        return None

    if phase in _VERIFICATION_PHASES:
        # Opus on a verification phase is the escalation tier — every
        # initial verification call routes to Sonnet by default.
        if model in OPUS_MODELS:
            return {"effort": EFFORT_HIGH}
        return {"effort": EFFORT_MEDIUM}

    level = _PHASE_DEFAULT_EFFORT.get(phase)
    if level is None:
        return None
    return {"effort": level}


def apply_effort_config(kwargs: dict, *, model: str, phase: str) -> dict:
    """Insert ``output_config`` into ``kwargs`` only when applicable.

    Mutates and returns ``kwargs`` for fluent use. The key is omitted
    entirely (not set to ``None``) when effort is not applicable, because
    the Anthropic API rejects ``output_config=null``.

    Mirrors :func:`apply_thinking_config` so request builders pair the
    two helpers the same way per directive 4 ("Pair effort decisions
    with thinking decisions where appropriate").
    """
    config = effort_config_for(model=model, phase=phase)
    if config is not None:
        kwargs["output_config"] = config
    return kwargs


# ---------------------------------------------------------------------------
# Prompt caching (centralized phase-aware policy)
# ---------------------------------------------------------------------------
#
# Each phase declares whether its system prompt and tool list are stable /
# large / repeated enough to benefit from caching. Caching is enabled for
# high-value phases (review, batch review, cross-check, verification +
# retry/continuation) and disabled for triage where the prompt is below
# the Anthropic cache minimum (2048 tokens for Haiku) so a cache write
# would be paid for nothing.


@dataclass(frozen=True)
class CachePolicy:
    """Per-phase cache policy.

    ``cache_system`` and ``cache_tools`` independently control whether the
    system prompt and the trailing tool block carry ``cache_control``
    breakpoints.
    """

    cache_system: bool
    cache_tools: bool

    @property
    def caches_anything(self) -> bool:
        return self.cache_system or self.cache_tools


_DEFAULT_PHASE_CACHE_POLICY = CachePolicy(cache_system=True, cache_tools=True)

_PHASE_CACHE_POLICY: dict[str, CachePolicy] = {
    PHASE_REVIEW: CachePolicy(cache_system=True, cache_tools=True),
    PHASE_BATCH_REVIEW: CachePolicy(cache_system=True, cache_tools=True),
    PHASE_CROSS_CHECK: CachePolicy(cache_system=True, cache_tools=True),
    PHASE_VERIFICATION: CachePolicy(cache_system=True, cache_tools=True),
    PHASE_VERIFICATION_RETRY: CachePolicy(cache_system=True, cache_tools=True),
    PHASE_VERIFICATION_CONTINUATION: CachePolicy(cache_system=True, cache_tools=True),
    # Triage: ~375-token system prompt called in batches of up to 20,
    # below the 2048-token Haiku cache minimum so repeated calls cannot
    # hit. Skip caching to avoid the cache-write cost.
    PHASE_TRIAGE: CachePolicy(cache_system=False, cache_tools=False),
}


def cache_policy_for(phase: str | None) -> CachePolicy:
    """Return the per-phase :class:`CachePolicy`.

    Unknown phases fall back to the conservative default (cache both
    system prompt and tools).
    """
    if phase is None:
        return _DEFAULT_PHASE_CACHE_POLICY
    return _PHASE_CACHE_POLICY.get(phase, _DEFAULT_PHASE_CACHE_POLICY)


def _cache_control_block() -> dict:
    """Return the standard 1-hour ephemeral cache_control block.

    Spec Critic batch + verification waves run for 30 minutes to several
    hours, well beyond the 5-minute default ephemeral cache TTL. The
    1-hour TTL costs 2x the cache write but typically pays back inside
    the second wave of a batch verification cycle, where the same system
    prompt is sent hundreds of times.
    """
    return {"type": "ephemeral", "ttl": "1h"}


def system_prompt_with_cache(prompt: str, *, phase: str | None = None):
    """Return a system payload with a cache breakpoint when policy permits.

    Per the Anthropic prompt-caching docs, including the same cache_control
    blocks in every request in a batch lets later items hit the cache
    created by earlier items.
    """
    policy = cache_policy_for(phase)
    if not policy.cache_system:
        return prompt
    return [
        {
            "type": "text",
            "text": prompt,
            "cache_control": _cache_control_block(),
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
    caching for the phase (e.g. triage where the prompt is below the cache
    minimum), the tool list is returned unchanged.
    """
    if not tools:
        return tools
    policy = cache_policy_for(phase)
    if not policy.cache_tools:
        return tools
    last = dict(tools[-1])
    last["cache_control"] = _cache_control_block()
    return [*tools[:-1], last]


# ---------------------------------------------------------------------------
# Service tier (priority capacity)
# ---------------------------------------------------------------------------


def batch_service_tier() -> str:
    """Return the ``service_tier`` parameter for batch request params.

    ``auto`` opts batch requests into priority capacity when available,
    falling back to standard.
    """
    return "auto"


# ---------------------------------------------------------------------------
# Anthropic token-counting preflight
# ---------------------------------------------------------------------------

def token_count_preflight_enabled() -> bool:
    """Whether to call Anthropic's count_tokens endpoint before submission.

    Always True. The GUI also runs an exact count for the largest spec
    when the file list changes; the pipeline call here is the moment-of-
    truth guard before a real submission.
    """
    return True


# ---------------------------------------------------------------------------
# Web-search tool configuration
# ---------------------------------------------------------------------------

# Source-quality blocklist for ``web_search_20260209``. Mixing
# ``allowed_domains`` and ``blocked_domains`` is not supported by the tool,
# so this is blocked-only; California priority sources are documented in the
# verifier system prompt as guidance rather than encoded as an allow-list.
#
# Domains are listed bare (no scheme/path) and the tool treats each entry as
# "this apex and every subdomain", so adding ``simple.wikipedia.org`` when
# ``wikipedia.org`` is already on the list adds nothing.
#
# Categories (kept as inline comment groups so the intent of each line is
# obvious; do *not* hand-sort across categories without checking that no
# entry's category interpretation changes):
#   - Aggregators / Q&A: forums where contractor-grade evidence is rare.
#   - LLM-assistant outputs: another model's answer is not a citable source.
#   - Trade forums: useful peer chatter, not authoritative for code.
#   - DIY / home-improvement content farms.
#   - Social: unsuitable for a defensible engineering review.
#   - General encyclopedias: tertiary sources.
#
# TODO: explore a category-based blocking helper so each entry is annotated
# with its category and the report can explain *why* a citation was rejected
# (Chunk 13 deferred this; the immediate fix here is just deduplicating the
# obvious subdomain overlap). Any change to this list should be exercised
# against the verifier's grounding tests in
# ``tests/test_chunk_h_source_grounding.py``.
_WEB_SEARCH_BLOCKED_DOMAINS = [
    # Aggregators / Q&A
    "reddit.com", "quora.com", "medium.com",
    "stackexchange.com", "stackoverflow.com",
    "answers.yahoo.com", "fixya.com",
    # LLM-assistant outputs
    "chatgpt.com", "perplexity.ai", "openai.com", "gemini.google.com",
    "claude.ai", "you.com", "phind.com", "copilot.microsoft.com",
    "poe.com", "character.ai", "jasper.ai", "writesonic.com",
    # Trade forums (peer chatter, not authoritative for code compliance)
    "diychatroom.com", "forums.jlconline.com", "hvac-talk.com",
    "inspectionnews.net", "inspectorsforum.com", "contractortalk.com",
    # DIY / home-improvement / lead-gen content farms
    "doityourself.com", "homeadvisor.com", "thumbtack.com", "angi.com",
    "ehow.com", "wikihow.com", "about.com", "thespruce.com", "bobvila.com",
    "familyhandyman.com", "hunker.com", "sapling.com", "reference.com",
    "leaf.tv", "sciencing.com", "bizfluent.com", "pocketsense.com",
    # Social
    "facebook.com", "twitter.com", "x.com", "instagram.com", "tiktok.com",
    "linkedin.com", "pinterest.com", "youtube.com", "threads.net",
    # General encyclopedias (tertiary). ``wikipedia.org`` already covers
    # every subdomain (``simple.wikipedia.org``, ``en.wikipedia.org``, ...).
    "wikipedia.org", "britannica.com",
]

# Fallback budget for severities outside the known set.
DEFAULT_VERIFICATION_MAX_USES = 5

# Per-severity search budgets. High-stakes claims get more rope; editorial
# gripes get less. Applied identically to real-time and batch verification
# paths so the budget shape doesn't depend on which mode you ran in.
_SEVERITY_MAX_USES: dict[str, int] = {
    "CRITICAL": 8,
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
