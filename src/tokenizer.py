"""
Token counting and limit management for Claude API calls.

Uses tiktoken with cl100k_base for approximate preflight estimates.
These counts are used for guardrails, not exact billing.

Token limits (v2.2.0):
    - Claude Opus 4.6 / Sonnet 4.6 context window: 1,000,000 tokens
    - Opus 4.6 max output: 128,000 tokens
    - Sonnet 4.6 max output: 64,000 tokens
    - Per-spec recommended input limit: 150,000 tokens
      (practical limit — individual specs are reviewed one at a time)
    - Cross-check recommended input limit: 900,000 tokens
      (uses the full 1M context for multi-spec coordination analysis)

The per-spec limit (RECOMMENDED_MAX) is intentionally conservative.
Even though the model supports 1M tokens, per-spec review calls send
a single spec at a time. Individual specs rarely exceed 50K tokens,
and the 150K ceiling provides a comfortable guardrail. The token gauge
in the GUI displays capacity against this per-spec limit.

The cross-check limit (CROSS_CHECK_RECOMMENDED_MAX) is much higher
because the cross-checker sends ALL spec content in a single call.
"""
import tiktoken
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Model limits
# ---------------------------------------------------------------------------

# Claude Opus 4.6 / Sonnet 4.6 context window (GA as of March 13, 2026)
# No beta header required. Standard pricing across the full window.
MAX_CONTEXT_TOKENS = 1_000_000

# Opus 4.6 max output tokens
MAX_OUTPUT_TOKENS_OPUS = 128_000


# ---------------------------------------------------------------------------
# Per-spec review limits (used by GUI token gauge and per-spec pipeline)
# ---------------------------------------------------------------------------

# Practical per-call input limit for per-spec reviews.
# Individual specs are reviewed one at a time — this is the budget for a
# single (system prompt + project context + spec content) API call.
# Conservative relative to the 1M window, but individual specs rarely
# exceed 50K tokens and this provides a sensible guardrail.
PER_SPEC_SAFETY_BUFFER = 50_000
RECOMMENDED_MAX = 150_000

# Padding for per-call overhead (message framing, file delimiter, safety margin)
PER_CALL_PADDING = 200


# ---------------------------------------------------------------------------
# Cross-check limits (v2.2.0)
# ---------------------------------------------------------------------------

# Cross-check uses Opus 4.6 with full spec content and adaptive thinking.
# With thinking enabled, thinking tokens + text output share the max_tokens budget.
# Opus 4.6 supports up to 128K output tokens. We use 64K to balance thinking
# depth with input capacity (more input room = more spec content).
# Budget: 1M context - 64K output reserve - 50K overhead = ~886K
# Rounded down to 880K for safety.
CROSS_CHECK_OUTPUT_BUDGET = 65_536
CROSS_CHECK_OVERHEAD = 50_000
CROSS_CHECK_RECOMMENDED_MAX = (
    MAX_CONTEXT_TOKENS - CROSS_CHECK_OUTPUT_BUDGET - CROSS_CHECK_OVERHEAD
)


def exceeds_per_call_limit(spec_tokens: int, overhead_tokens: int) -> bool:
    """Check if a single spec would exceed the per-call token limit."""
    return (overhead_tokens + spec_tokens + PER_CALL_PADDING) > RECOMMENDED_MAX


def get_encoder():
    """Get the tokenizer used for approximate token estimates."""
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Count tokens in a text string."""
    encoder = get_encoder()
    return len(encoder.encode(text))


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