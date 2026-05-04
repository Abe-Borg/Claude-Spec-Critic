"""
Token counting and limit management for Claude API calls.

Uses tiktoken with cl100k_base for approximate preflight estimates.
These counts are used for guardrails, not exact billing.

Token limits (v2.3.0):
    - Claude Opus 4.7 context window: 1,000,000 tokens
    - Opus 4.7 max output: 128,000 tokens
    - Sonnet 4.6 max output: 64,000 tokens
    - Per-spec recommended input limit: 500,000 tokens
      (practical limit — individual specs are reviewed one at a time)
    - Cross-check recommended input limit: ~822,000 tokens
      (1,000,000 context - 128,000 output reserve - 50,000 overhead)

The per-spec limit (RECOMMENDED_MAX) is intentionally conservative
relative to the 1M context window. Per-spec review calls send a single
spec at a time, and the token gauge in the GUI displays the largest
spec's call size against this limit.

The cross-check limit (CROSS_CHECK_RECOMMENDED_MAX) is much higher
because the cross-checker sends ALL spec content in a single call.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import tiktoken

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model limits
# ---------------------------------------------------------------------------

# Claude Opus 4.7 context window (1M tokens, no beta header required).
MAX_CONTEXT_TOKENS = 1_000_000

# Opus 4.7 max output tokens
MAX_OUTPUT_TOKENS_OPUS = 128_000
# Sonnet 4.6 max output tokens
MAX_OUTPUT_TOKENS_SONNET = 64_000


# ---------------------------------------------------------------------------
# Per-spec review limits (used by GUI token gauge and per-spec pipeline)
# ---------------------------------------------------------------------------

# Practical per-call input limit for per-spec reviews.
# Individual specs are reviewed one at a time — this is the budget for a
# single (system prompt + project context + spec content) API call.
# Conservative relative to the 1M window and intended as a practical guardrail.
RECOMMENDED_MAX = 500_000

# Hard cap on the Project Context block. The context is sent on every per-spec
# review call, every cross-check call, and every verification call, so it
# multiplies cost quickly. 100K tokens leaves ~400K of the per-spec budget for
# the spec itself.
PROJECT_CONTEXT_MAX_TOKENS = 100_000


# ---------------------------------------------------------------------------
# Cross-check limits (v2.2.0)
# ---------------------------------------------------------------------------

# Cross-check uses Opus 4.7 with full spec content and adaptive thinking.
# With thinking enabled, thinking tokens + text output share the max_tokens budget.
# Opus 4.7 supports up to 128K output tokens. Cross-check reserves the
# full output budget so adaptive thinking + text output can use it.
# Budget: 1M context - 128K output reserve - 50K overhead = 822K
CROSS_CHECK_OVERHEAD = 50_000
CROSS_CHECK_OUTPUT_BUDGET = 128_000
CROSS_CHECK_RECOMMENDED_MAX = (
    MAX_CONTEXT_TOKENS - CROSS_CHECK_OUTPUT_BUDGET - CROSS_CHECK_OVERHEAD
)


def exceeds_per_call_limit(spec_tokens: int, overhead_tokens: int) -> bool:
    """Check if a single spec would exceed the per-call token limit."""
    return (overhead_tokens + spec_tokens) > RECOMMENDED_MAX


def get_encoder():
    """Get the tokenizer used for approximate token estimates."""
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Count tokens in a text string (local cl100k_base estimate)."""
    encoder = get_encoder()
    return len(encoder.encode(text))


def count_tokens_via_api(
    *,
    model: str,
    system: Any,
    messages: list[dict],
    tools: Optional[list[dict]] = None,
    client: Any = None,
) -> Optional[int]:
    """Exact token count via Anthropic's count_tokens endpoint.

    Returns the input-token total for the given request shape, or ``None`` on
    failure (network error, missing API key, SDK version mismatch). Callers
    should treat ``None`` as "preflight unavailable" and fall back to the
    local estimate rather than blocking submission.

    Plan section 6.3: keep the local estimate for UI responsiveness, use this
    helper before batch submission and real-time confirmation when exact
    routing/guardrail decisions matter.
    """
    if client is None:
        try:
            from .reviewer import _get_client
            client = _get_client()
        except Exception as exc:  # pragma: no cover - exercised via tests
            _log.warning("count_tokens_via_api: no client available (%s)", exc)
            return None
    try:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if system is not None:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        result = client.messages.count_tokens(**kwargs)
        # MessageTokensCount has an input_tokens attribute.
        return int(getattr(result, "input_tokens", 0) or 0)
    except Exception as exc:
        _log.warning("count_tokens_via_api failed: %s", exc)
        return None


def analyze_token_usage(
    spec_contents: list[tuple[str, str]], 
    system_prompt: str
) -> 'TokenSummary':
    """Analyze token usage for a set of specs and system prompt."""
    encoder = get_encoder()
    
    system_tokens = len(encoder.encode(system_prompt))
    
    items = []
    for filename, content in spec_contents:
        tokens = len(encoder.encode(content))
        items.append(TokenCount(
            name=filename,
            tokens=tokens,
            chars=len(content)
        ))
    
    content_tokens = sum(item.tokens for item in items)
    total_tokens = system_tokens + content_tokens
    
    within_limit = total_tokens <= RECOMMENDED_MAX
    warning_message = None
    
    if total_tokens > MAX_CONTEXT_TOKENS:
        warning_message = (
            f"CRITICAL: Total tokens ({total_tokens:,}) exceeds maximum context "
            f"({MAX_CONTEXT_TOKENS:,}). Review cannot proceed."
        )
    elif total_tokens > RECOMMENDED_MAX:
        warning_message = (
            f"WARNING: Total tokens ({total_tokens:,}) exceeds recommended limit "
            f"({RECOMMENDED_MAX:,}). Response may be truncated."
        )
    elif total_tokens > RECOMMENDED_MAX * 0.8:
        warning_message = (
            f"Note: Using {total_tokens:,} of {RECOMMENDED_MAX:,} recommended tokens "
            f"({total_tokens / RECOMMENDED_MAX * 100:.0f}%). Approaching limit."
        )
    
    return TokenSummary(
        items=items,
        system_prompt_tokens=system_tokens,
        total_tokens=total_tokens,
        within_limit=within_limit,
        warning_message=warning_message
    )


@dataclass
class TokenCount:
    """Token count for a single piece of content."""
    name: str
    tokens: int
    chars: int
    

@dataclass  
class TokenSummary:
    """Complete token analysis for a review job."""
    items: list[TokenCount]
    system_prompt_tokens: int
    total_tokens: int
    within_limit: bool
    warning_message: str | None
    
    @property
    def content_tokens(self) -> int:
        return sum(item.tokens for item in self.items)


def format_token_summary(summary: TokenSummary) -> str:
    """Format a token summary for display."""
    lines = ["Token Usage:"]
    
    for item in summary.items:
        lines.append(f"  * {item.name}: {item.tokens:,} tokens ({item.chars:,} chars)")
    
    lines.append(f"  System prompt: {summary.system_prompt_tokens:,} tokens")
    lines.append(f"  Total: {summary.total_tokens:,} / {RECOMMENDED_MAX:,} tokens")
    
    if summary.warning_message:
        lines.append(f"\n  {summary.warning_message}")
    else:
        lines.append(f"  Within recommended limits")
    
    return "\n".join(lines)
