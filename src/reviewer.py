"""
Claude API client for specification review.

This module handles all communication with the Anthropic API, including:
    - Streaming responses for real-time GUI updates
    - Parsing JSON findings from Claude's response
    - Retry logic for transient errors (rate limits, connection issues)
    - Token usage tracking

Model:
    This tool uses Claude Opus 4.5 exclusively. The model is hardcoded —
    there are no flags to select different models. Opus 4.5 was chosen for
    its superior reasoning on complex technical documents.

Response format:
    Claude returns a two-part response:
        1. Analysis summary (free-form text with personality)
        2. JSON array of findings
    
    The _extract_json_array() function splits these apart. The summary
    goes into ReviewResult.thinking; the findings are parsed into
    ReviewResult.findings.

Streaming:
    The stream_callback parameter enables real-time display of Claude's
    response as it generates. Each text chunk is passed to the callback
    immediately. This is used by the GUI's StreamingPanel.

Error handling:
    - RateLimitError: Exponential backoff (10s, 20s, 40s)
    - APIConnectionError: Exponential backoff (5s, 10s, 20s)
    - APIError: Immediate failure (no retry)
    - JSON parse errors: Captured in ReviewResult.error

Usage:
    from reviewer import review_specs, ReviewResult
    
    result = review_specs(
        combined_content="===== FILE: spec.docx =====\\n...",
        stream_callback=lambda chunk: print(chunk, end=""),
    )
    
    if result.error:
        print(f"Failed: {result.error}")
    else:
        print(f"Found {result.total_count} issues")
"""
from __future__ import annotations

import os
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from anthropic import Anthropic, APIError, APIConnectionError, RateLimitError

from .prompts import get_system_prompt, get_user_message


# Single allowed model for this repo
MODEL_OPUS_45 = "claude-opus-4-5-20251101"

# Type alias for streaming callback
# Called with each text chunk as it arrives from the API
StreamCallback = Callable[[str], None]


@dataclass
class Finding:
    """
    A single review finding from Claude's analysis.
    
    Maps directly to the JSON schema defined in prompts.py. All fields
    are strings (or None) to simplify serialization.
    
    Attributes:
        severity: CRITICAL | HIGH | MEDIUM | GRIPES
        fileName: Source spec filename (from ===== FILE: header)
        section: CSI location (e.g., "Part 2, Article 2.1.B.3")
        issue: Description of the problem and why it matters
        actionType: ADD | EDIT | DELETE
        existingText: Current problematic text (None if ADD)
        replacementText: Corrected text (None if DELETE)
        codeReference: Violated code/standard (None if editorial)
    """
    severity: str
    fileName: str
    section: str
    issue: str
    actionType: str
    existingText: str | None
    replacementText: str | None
    codeReference: str | None


@dataclass
class ReviewResult:
    """
    Complete result of a specification review API call.
    
    Contains parsed findings, raw response for debugging, token usage
    metrics, and any error message if the call failed.
    
    Attributes:
        findings: List of parsed Finding objects
        raw_response: Complete text response from Claude (for debugging)
        thinking: Analysis summary text before the JSON (Claude's commentary)
        model: Model identifier used for the request
        input_tokens: Tokens in the request (from API usage stats)
        output_tokens: Tokens in the response (from API usage stats)
        elapsed_seconds: Wall-clock time for the API call
        error: Error message if call failed, None if successful
    """
    findings: list[Finding] = field(default_factory=list)
    raw_response: str = ""
    thinking: str = ""  # Claude's analysis summary before the JSON output
    model: str = MODEL_OPUS_45
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_seconds: float = 0.0
    error: str | None = None

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "CRITICAL")

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "HIGH")

    @property
    def medium_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "MEDIUM")

    @property
    def gripe_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "GRIPES")

    @property
    def total_count(self) -> int:
        return len(self.findings)


def _get_api_key() -> str:
    """
    Retrieve API key from environment variable.
    
    Returns:
        The ANTHROPIC_API_KEY value
        
    Raises:
        ValueError: If environment variable is not set
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("ANTHROPIC_API_KEY environment variable not set")
    return key


def _extract_json_array(text: str) -> tuple[list, str]:
    """
    Extract the JSON findings array and analysis summary from Claude's response.
    
    Claude's response format (per prompts.py) is:
        <analysis summary with personality>
        
        [
          {"severity": "CRITICAL", ...},
          ...
        ]
    
    This function finds the outermost [ ] brackets and splits the response
    into the thinking text (before [) and the JSON array.
    
    Args:
        text: Raw response text from Claude
        
    Returns:
        Tuple of (parsed_json_list, thinking_text)
        
    Raises:
        ValueError: If no valid JSON array found in response
    """
    start_idx = text.find("[")
    end_idx = text.rfind("]")
    
    if start_idx == -1 or end_idx == -1:
        if "no issues" in text.lower() or text.strip() == "[]":
            return [], text.strip()
        raise ValueError(f"Could not find JSON array in response: {text[:200]}...")

    # Capture any text before the JSON array as "thinking"
    thinking = text[:start_idx].strip()
    
    json_str = text[start_idx:end_idx + 1]
    data = json.loads(json_str)
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array, got: {type(data)}")
    return data, thinking


def _parse_findings(data: list) -> list[Finding]:
    """
    Parse raw JSON dicts into Finding dataclass instances.
    
    Handles missing or malformed fields gracefully — uses empty strings
    and None rather than raising exceptions. This is important because
    Claude occasionally omits optional fields.
    
    Args:
        data: List of dicts from JSON response
        
    Returns:
        List of Finding objects
    """
    findings: list[Finding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        findings.append(
            Finding(
                severity=str(item.get("severity", "")).strip(),
                fileName=str(item.get("fileName", "")).strip(),
                section=str(item.get("section", "")).strip(),
                issue=str(item.get("issue", "")).strip(),
                actionType=str(item.get("actionType", "")).strip(),
                existingText=item.get("existingText", None),
                replacementText=item.get("replacementText", None),
                codeReference=item.get("codeReference", None),
            )
        )
    return findings


# -----------------------------------------------------------------------------
# Main API Function
# -----------------------------------------------------------------------------


def review_specs(
    combined_content: str,
    *,
    max_retries: int = 3,
    verbose: bool = False,
    stream_callback: Optional[StreamCallback] = None,
) -> ReviewResult:
    """
    Send specifications to Claude for review and parse the response.
    
    This is the main API entry point. It:
        1. Builds the request with system prompt and user message
        2. Calls the streaming API endpoint
        3. Accumulates the response while calling stream_callback
        4. Parses the JSON findings from the response
        5. Returns a ReviewResult with findings and metadata
    
    Retry behavior:
        - RateLimitError: Exponential backoff (10s * 2^attempt)
        - APIConnectionError: Exponential backoff (5s * 2^attempt)
        - Other APIError: No retry, immediate failure
        - Max 3 attempts by default
    
    Args:
        combined_content: Combined specification text with file delimiters.
                          Format: "===== FILE: name.docx =====\\n<content>"
        max_retries: Maximum retry attempts for transient errors
        verbose: If True, print status messages to stdout
        stream_callback: Optional function called with each text chunk as
                         Claude generates it. Enables real-time display.
                         Exceptions in callback are silently ignored to
                         prevent UI errors from breaking the API call.
    
    Returns:
        ReviewResult containing:
            - findings: Parsed Finding objects
            - thinking: Claude's analysis summary
            - raw_response: Complete response text
            - Token counts and timing
            - error: Error message if failed, None if successful
        
    Example:
        >>> result = review_specs(
        ...     combined_content="===== FILE: test.docx =====\\nPart 1...",
        ...     stream_callback=lambda c: print(c, end="", flush=True),
        ... )
        >>> if not result.error:
        ...     for f in result.findings:
        ...         print(f"{f.severity}: {f.issue}")
    """
    start_time = time.time()
    result = ReviewResult(model=MODEL_OPUS_45)

    client = Anthropic(api_key=_get_api_key())

    system_prompt = get_system_prompt()
    user_message = get_user_message(combined_content)

    max_tokens = 32768  # adjust if desired

    for attempt in range(max_retries):
        try:
            if verbose:
                print(f"Calling Claude (attempt {attempt + 1}/{max_retries})...")

            # Use streaming to enable real-time display
            with client.messages.stream(
                model=MODEL_OPUS_45,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            ) as stream:
                
                # Accumulate full response while streaming
                response_chunks: list[str] = []
                
                for text in stream.text_stream:
                    response_chunks.append(text)
                    
                    # Call the streaming callback if provided
                    if stream_callback:
                        try:
                            stream_callback(text)
                        except Exception:
                            # Don't let callback errors break the stream
                            pass
                
                # Get the final message for token counts
                resp = stream.get_final_message()
            
            # Reconstruct full response from chunks
            response_text = "".join(response_chunks)
            result.raw_response = response_text

            # Token usage (best effort)
            try:
                usage = getattr(resp, "usage", None)
                if usage:
                    result.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                    result.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            except Exception:
                pass
            
            # Parse response into findings
            data, thinking = _extract_json_array(response_text)
            result.findings = _parse_findings(data)
            result.thinking = thinking
            result.elapsed_seconds = time.time() - start_time
            return result

        except RateLimitError as e:
            # Exponential backoff: 10s, 20s, 40s
            wait_time = 2 ** attempt * 10
            if verbose:
                print(f"Rate limit: {e}. Waiting {wait_time}s...")
            time.sleep(wait_time)

        except APIConnectionError as e:
            # Exponential backoff: 5s, 10s, 20s
            wait_time = 2 ** attempt * 5
            if verbose:
                print(f"Connection error: {e}. Waiting {wait_time}s...")
            time.sleep(wait_time)

        except APIError as e:
            # Non-retryable API error
            result.error = f"API error: {e}"
            result.elapsed_seconds = time.time() - start_time
            return result

        except Exception as e:
            # Catch-all for JSON parsing errors, etc.
            result.error = f"Error: {e}"
            result.elapsed_seconds = time.time() - start_time
            return result

    # Exhausted all retries
    result.error = f"Failed after {max_retries} attempts."
    result.elapsed_seconds = time.time() - start_time
    return result