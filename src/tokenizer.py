"""
Token counting and limit management for Claude API calls.

This module provides token counting using tiktoken (the same tokenizer
used by Claude) and enforces context window limits before API calls.
Pre-flight token checking prevents wasted API calls and provides clear
feedback when specs need to be split into batches.

Token limits:
    - Claude Opus 4.6 context window: 200,000 tokens
    - Recommended input limit: 150,000 tokens
    - The 50k buffer leaves room for:
        - System prompt (~2-3k tokens)
        - Max output response (32,768 tokens)
        - Safety margin for tokenizer differences

Tokenizer:
    Uses tiktoken with cl100k_base encoding. This is the same base
    encoding used by Claude models, so counts are accurate within
    a small margin.

Usage:
    from tokenizer import analyze_token_usage, count_tokens, RECOMMENDED_MAX
    
    # Quick single-string count
    tokens = count_tokens("Some specification text...")
    
    # Full analysis with breakdown
    specs = [("spec1.docx", content1), ("spec2.docx", content2)]
    summary = analyze_token_usage(specs, system_prompt)
    
    if not summary.within_limit:
        print(summary.warning_message)
"""
import tiktoken
from dataclasses import dataclass


MAX_CONTEXT_TOKENS = 200_000

# Buffer for system prompt + response + safety margin
# System prompt: ~2-3k tokens
# Max output: 32,768 tokens
# Safety margin: ~15k tokens
SAFETY_BUFFER = 50_000

# Recommended maximum input tokens (exported for use by other modules)
RECOMMENDED_MAX = MAX_CONTEXT_TOKENS - SAFETY_BUFFER  


@dataclass
class TokenCount:
    """
    Token count for a single piece of content.
    
    Attributes:
        name: Identifier (typically filename)
        tokens: Exact token count from tiktoken
        chars: Character count (for reference)
    """
    name: str
    tokens: int
    chars: int
    

@dataclass  
class TokenSummary:
    """
    Complete token analysis for a review job.
    
    Provides both detailed per-file breakdown and aggregate totals,
    along with a pre-computed within_limit flag and warning message.
    
    Attributes:
        items: List of TokenCount objects (one per input file)
        system_prompt_tokens: Tokens in the system prompt
        total_tokens: Sum of system prompt + all content tokens
        within_limit: True if total_tokens <= RECOMMENDED_MAX
        warning_message: Human-readable warning/status (None if well under limit)
    """
    items: list[TokenCount]
    system_prompt_tokens: int
    total_tokens: int
    within_limit: bool
    warning_message: str | None
    
    @property
    def content_tokens(self) -> int:
        return sum(item.tokens for item in self.items)


# -----------------------------------------------------------------------------
# Core Functions
# -----------------------------------------------------------------------------

def get_encoder():
    """
    Get the tiktoken encoder for Claude models.
    
    Claude uses cl100k_base encoding, which is the same base encoding
    used by GPT-4 and other modern LLMs. Token counts are accurate
    within a small margin.
    
    Returns:
        tiktoken.Encoding instance
        
    Note:
        The encoder is not cached here — tiktoken handles caching
        internally. Repeated calls are fast.
    """
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """
    Count tokens in a text string.
    
    Uses tiktoken cl100k_base encoding for accurate counts.
    
    Args:
        text: The text to count tokens for
        
    Returns:
        Number of tokens
        
    Example:
        >>> count_tokens("Hello, world!")
        4
    """
    encoder = get_encoder()
    return len(encoder.encode(text))


def estimate_tokens_from_chars(char_count: int) -> int:
    """
    Rough estimate of tokens from character count.
    
    Useful for quick estimates without the overhead of encoding.
    Average is ~4 characters per token for English prose. Technical
    specifications may have slightly different ratios due to numbers,
    abbreviations, and formatting.
    
    Args:
        char_count: Number of characters
        
    Returns:
        Estimated token count (chars // 4)
        
    Note:
        This is a rough approximation. Use count_tokens() for accurate
        counts before API calls.
    """
    return char_count // 4


def analyze_token_usage(
    spec_contents: list[tuple[str, str]], 
    system_prompt: str
) -> TokenSummary:
    """
    Analyze token usage for a set of specs and system prompt.
    
    Provides detailed breakdown by file plus aggregate totals. Computes
    whether the total is within the recommended limit and generates
    appropriate warning messages.
    
    Warning levels:
        - > MAX_CONTEXT_TOKENS (200k): CRITICAL, cannot proceed
        - > RECOMMENDED_MAX (150k): WARNING, may truncate response
        - > 80% of RECOMMENDED_MAX (120k): Note, approaching limit
        - <= 80%: No warning
    
    Args:
        spec_contents: List of (filename, content) tuples
        system_prompt: The system prompt to be used
        
    Returns:
        TokenSummary with detailed breakdown and status
        
    Example:
        >>> specs = [("spec1.docx", text1), ("spec2.docx", text2)]
        >>> summary = analyze_token_usage(specs, system_prompt)
        >>> print(f"Total: {summary.total_tokens:,} tokens")
        >>> if not summary.within_limit:
        ...     print(f"Error: {summary.warning_message}")
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
        # Approaching limit — informational
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
    Format a token summary for CLI display.
    
    Produces a human-readable multi-line string with per-file breakdown,
    totals, and any warnings. Intended for terminal output.
    
    Args:
        summary: TokenSummary to format
        
    Returns:
        Formatted string ready for print()
        
    Example output:
        Token Usage:
          • spec1.docx: 12,345 tokens (45,678 chars)
          • spec2.docx: 8,901 tokens (32,456 chars)
          System prompt: 2,500 tokens
          ─────────────────────
          Total: 23,746 / 150,000 tokens
          ✓ Within recommended limits
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
