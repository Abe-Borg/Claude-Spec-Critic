"""Token counting and limit checking module."""
import tiktoken
from dataclasses import dataclass


MAX_CONTEXT_TOKENS = 200_000
SAFETY_BUFFER = 50_000  # Leave room for system prompt + response
RECOMMENDED_MAX = MAX_CONTEXT_TOKENS - SAFETY_BUFFER  


@dataclass
class TokenCount:
    """Token count for a single piece of content."""
    name: str
    tokens: int
    chars: int
    

@dataclass  
class TokenSummary:
    """Summary of token counts for a review job."""
    items: list[TokenCount]
    system_prompt_tokens: int
    total_tokens: int
    within_limit: bool
    warning_message: str | None
    
    @property
    def content_tokens(self) -> int:
        return sum(item.tokens for item in self.items)


def get_encoder():
    """Get the tiktoken encoder. Claude uses cl100k_base."""
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """
    Count tokens in a text string.
    
    Args:
        text: The text to count tokens for
        
    Returns:
        Number of tokens
    """
    encoder = get_encoder()
    return len(encoder.encode(text))


def estimate_tokens_from_chars(char_count: int) -> int:
    """
    Rough estimate of tokens from character count.
    Useful for quick estimates without encoding.
    
    Average is ~4 chars per token for English text.
    """
    return char_count // 4


def analyze_token_usage(
    spec_contents: list[tuple[str, str]], 
    system_prompt: str
) -> TokenSummary:
    """
    Analyze token usage for a set of specs and system prompt.
    
    Args:
        spec_contents: List of (filename, content) tuples
        system_prompt: The system prompt to be used
        
    Returns:
        TokenSummary with detailed breakdown and warnings
    """
    encoder = get_encoder()
    
    # Count system prompt tokens
    system_tokens = len(encoder.encode(system_prompt))
    
    # Count each spec
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
    
    # Determine if within limits and generate warning if needed
    within_limit = total_tokens <= RECOMMENDED_MAX
    warning_message = None
    
    if total_tokens > MAX_CONTEXT_TOKENS:
        warning_message = (
            f"CRITICAL: Total tokens ({total_tokens:,}) exceeds maximum context "
            f"({MAX_CONTEXT_TOKENS:,}). Review cannot proceed. "
            f"Remove some specifications or reduce content."
        )
    elif total_tokens > RECOMMENDED_MAX:
        warning_message = (
            f"WARNING: Total tokens ({total_tokens:,}) exceeds recommended limit "
            f"({RECOMMENDED_MAX:,}). Response may be truncated. "
            f"Consider removing some specifications."
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


def format_token_summary(summary: TokenSummary) -> str:
    """
    Format a token summary for display.
    
    Args:
        summary: TokenSummary to format
        
    Returns:
        Formatted string for CLI output
    """
    lines = ["Token Usage:"]
    
    for item in summary.items:
        lines.append(f"  • {item.name}: {item.tokens:,} tokens ({item.chars:,} chars)")
    
    lines.append(f"  System prompt: {summary.system_prompt_tokens:,} tokens")
    lines.append(f"  ─────────────────────")
    lines.append(f"  Total: {summary.total_tokens:,} / {RECOMMENDED_MAX:,} tokens")
    
    if summary.warning_message:
        lines.append(f"\n⚠️  {summary.warning_message}")
    else:
        lines.append(f"  ✓ Within recommended limits")
    
    return "\n".join(lines)
