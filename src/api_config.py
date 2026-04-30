"""Centralized Anthropic API configuration for Spec Critic.

Phase 2 (API modernization): single place for model identifiers, per-phase
output-token caps, batch beta headers, web-search tool configuration, and
feature flags for prompt caching and Anthropic token-counting preflight.

All knobs are read from environment variables with sane defaults. Existing
constants in `verification_config.py` and `tokenizer.py` are re-exported here
so future model migration touches one file.

Feature flags (all default-on except where noted):
    SPEC_CRITIC_PROMPT_CACHE        — "0" disables prompt caching.
    SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT — "1" enables Anthropic count_tokens
                                        preflight before submission. Default
                                        off (opt-in) so live API calls only
                                        happen when explicitly requested.
    SPEC_CRITIC_VERIFICATION_MAX_USES — int override for web-search max_uses.
    SPEC_CRITIC_VERIFICATION_MODEL  — model id override for verification.
    SPEC_CRITIC_REVIEW_MODEL        — model id override for review.
"""
from __future__ import annotations

import os
from typing import Iterable

# ---------------------------------------------------------------------------
# Model identifiers (centralized)
# ---------------------------------------------------------------------------

MODEL_OPUS_46 = "claude-opus-4-6"
MODEL_OPUS_47 = "claude-opus-4-7"
MODEL_SONNET_46 = "claude-sonnet-4-6"
MODEL_HAIKU_45 = "claude-haiku-4-5-20251001"

# Defaults preserved from prior behavior. Phase 3 will introduce Sonnet
# verification with Opus escalation; Phase 2 only adds the routing scaffold.
REVIEW_MODEL_DEFAULT = os.environ.get("SPEC_CRITIC_REVIEW_MODEL", MODEL_OPUS_46)
CROSS_CHECK_MODEL_DEFAULT = os.environ.get("SPEC_CRITIC_CROSS_CHECK_MODEL", MODEL_OPUS_46)
VERIFICATION_MODEL_DEFAULT = os.environ.get("SPEC_CRITIC_VERIFICATION_MODEL", MODEL_OPUS_46)

# Convenience set of "Opus-class" models for output-cap dispatch.
OPUS_MODELS = frozenset({MODEL_OPUS_46, MODEL_OPUS_47})


# ---------------------------------------------------------------------------
# Output-token caps
# ---------------------------------------------------------------------------

# Hard ceilings imposed by the model.
MAX_OUTPUT_TOKENS_OPUS = 128_000
MAX_OUTPUT_TOKENS_SONNET = 64_000

# Extended-output batch beta. Required header to use 300k output in batch.
BATCH_OUTPUT_BETA = "output-300k-2026-03-24"
BATCH_MAX_OUTPUT_TOKENS = 300_000

# Per-phase dynamic caps. These are intentionally lower than the hard model
# ceilings so the app does not blanket-allocate the maximum on every call.
# Plan section 6.6: "Reduce runaway cost exposure"; Sprint 2 item 4.
REVIEW_OUTPUT_CAP_REALTIME = 64_000   # streaming review of one spec
REVIEW_OUTPUT_CAP_BATCH = 128_000     # standard batch review
REVIEW_OUTPUT_CAP_BATCH_LARGE = 300_000  # only when 300k beta header is set
CROSS_CHECK_OUTPUT_CAP = 96_000       # cross-check needs more than verify
VERIFICATION_OUTPUT_CAP = 32_000      # verification verdicts are short

# Token threshold above which a review uses the larger batch cap.
LARGE_REVIEW_INPUT_THRESHOLD = 200_000


def output_cap_for_model(model: str, *, requested: int) -> int:
    """Clamp ``requested`` to the model's hard ceiling."""
    ceiling = MAX_OUTPUT_TOKENS_OPUS if model in OPUS_MODELS else MAX_OUTPUT_TOKENS_SONNET
    return min(requested, ceiling)


def review_max_tokens(*, batch: bool, model: str = REVIEW_MODEL_DEFAULT, input_tokens: int = 0, allow_extended_output: bool = False) -> int:
    """Return a per-call max_tokens for a review request.

    ``allow_extended_output`` must be True for the 300k batch path. The plan
    requires a fail-fast guard against using 300k without the beta header
    (Sprint 2 item 8); see :func:`assert_extended_output_allowed`.
    """
    if not batch:
        return output_cap_for_model(model, requested=REVIEW_OUTPUT_CAP_REALTIME)
    if allow_extended_output:
        return min(BATCH_MAX_OUTPUT_TOKENS, REVIEW_OUTPUT_CAP_BATCH_LARGE)
    if input_tokens >= LARGE_REVIEW_INPUT_THRESHOLD:
        # Larger inputs may need more headroom but stay within the model's
        # standard ceiling.
        return output_cap_for_model(model, requested=REVIEW_OUTPUT_CAP_BATCH)
    return output_cap_for_model(model, requested=REVIEW_OUTPUT_CAP_BATCH)


def cross_check_max_tokens(*, model: str = CROSS_CHECK_MODEL_DEFAULT) -> int:
    return output_cap_for_model(model, requested=CROSS_CHECK_OUTPUT_CAP)


def verification_max_tokens(*, model: str = VERIFICATION_MODEL_DEFAULT) -> int:
    return output_cap_for_model(model, requested=VERIFICATION_OUTPUT_CAP)


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
# Prompt caching
# ---------------------------------------------------------------------------

def prompt_caching_enabled() -> bool:
    """Whether to attach cache_control breakpoints to stable prompt prefixes."""
    return os.environ.get("SPEC_CRITIC_PROMPT_CACHE", "1") != "0"


def system_prompt_with_cache(prompt: str):
    """Return a system payload with an ephemeral cache breakpoint when enabled.

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
            "cache_control": {"type": "ephemeral"},
        }
    ]


def tools_with_cache(tools: list[dict]) -> list[dict]:
    """Attach an ephemeral cache breakpoint to the last tool definition.

    Tool schemas are stable across verification calls. Caching the trailing
    tool block lets the rest of the request (system prompt + tool defs)
    share one cache prefix.
    """
    if not prompt_caching_enabled() or not tools:
        return tools
    last = dict(tools[-1])
    last["cache_control"] = {"type": "ephemeral"}
    return [*tools[:-1], last]


# ---------------------------------------------------------------------------
# Anthropic token-counting preflight (opt-in)
# ---------------------------------------------------------------------------

def token_count_preflight_enabled() -> bool:
    return os.environ.get("SPEC_CRITIC_TOKEN_COUNT_PREFLIGHT", "0") == "1"


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

# Lowered from prior 10. Plan section 6.8 recommends reducing default
# max_uses for simple verification and escalating only when needed.
DEFAULT_VERIFICATION_MAX_USES = int(
    os.environ.get("SPEC_CRITIC_VERIFICATION_MAX_USES", "5")
)


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


# Default web-search tool used when no per-call override is needed.
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
