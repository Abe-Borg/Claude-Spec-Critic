"""Claude API client for specification review."""
import os
import json
import time
from dataclasses import dataclass, field
from anthropic import Anthropic, APIError, APIConnectionError, RateLimitError

from .prompts import get_system_prompt, get_user_message


# Model constants
MODEL_SONNET = "claude-sonnet-4-5-20250929"
MODEL_OPUS = "claude-opus-4-5-20251101"


@dataclass
class Finding:
    """A single review finding."""
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
    """Result of a specification review."""
    findings: list[Finding] = field(default_factory=list)
    raw_response: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
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
    def low_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "LOW")
    
    @property
    def gripes_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "GRIPES")
    
    @property
    def total_count(self) -> int:
        return len(self.findings)
    
    @property
    def total_output_tokens(self) -> int:
        """Total output tokens including thinking tokens."""
        return self.output_tokens + self.thinking_tokens


def get_api_key() -> str:
    """Get the Anthropic API key from environment."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError(
            "ANTHROPIC_API_KEY environment variable not set.\n"
            "Set it with: set ANTHROPIC_API_KEY=your-key-here (Windows CMD)\n"
            "         or: $env:ANTHROPIC_API_KEY='your-key-here' (PowerShell)"
        )
    return key


def parse_findings(response_text: str) -> list[Finding]:
    """
    Parse the JSON response from Claude into Finding objects.
    
    Args:
        response_text: Raw text response from the API
        
    Returns:
        List of Finding objects
        
    Raises:
        ValueError: If response cannot be parsed as JSON
    """
    # Try to extract JSON array from response
    text = response_text.strip()
    
    # Remove markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```)
        lines = lines[1:]
        # Remove last line (```)
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    
    # Try to find JSON array in the text
    start_idx = text.find("[")
    end_idx = text.rfind("]")
    
    if start_idx == -1 or end_idx == -1:
        # No array found - might be empty or error
        if "no issues" in text.lower() or text.strip() == "[]":
            return []
        raise ValueError(f"Could not find JSON array in response: {text[:200]}...")
    
    json_str = text[start_idx:end_idx + 1]
    
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in response: {e}")
    
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array, got: {type(data)}")
    
    findings = []
    for item in data:
        try:
            finding = Finding(
                severity=item.get("severity", "MEDIUM"),
                fileName=item.get("fileName", "Unknown"),
                section=item.get("section", "Unknown"),
                issue=item.get("issue", "No description"),
                actionType=item.get("actionType", "EDIT"),
                existingText=item.get("existingText"),
                replacementText=item.get("replacementText"),
                codeReference=item.get("codeReference")
            )
            findings.append(finding)
        except Exception as e:
            # Skip malformed findings but continue processing
            continue
    
    return findings


def review_specs(
    combined_content: str,
    model: str = MODEL_SONNET,
    use_thinking: bool = False,
    max_retries: int = 3,
    verbose: bool = False
) -> ReviewResult:
    """
    Send specifications to Claude for review.
    
    Args:
        combined_content: Combined specification text with file delimiters
        model: Claude model to use (MODEL_SONNET or MODEL_OPUS)
        use_thinking: Whether to enable extended thinking (Opus only)
        max_retries: Number of retry attempts for transient errors
        verbose: Whether to print progress messages
        
    Returns:
        ReviewResult with findings and metadata
    """
    result = ReviewResult(model=model)
    
    # Extended thinking only works with Opus
    if use_thinking and model != MODEL_OPUS:
        model = MODEL_OPUS
        result.model = MODEL_OPUS
        if verbose:
            print("  Note: Extended thinking requires Opus. Switching to Opus.")
    
    try:
        api_key = get_api_key()
    except ValueError as e:
        result.error = str(e)
        return result
    
    client = Anthropic(api_key=api_key)
    system_prompt = get_system_prompt()
    user_message = get_user_message(combined_content)
    
    last_error = None
    start_time = time.time()
    
    for attempt in range(max_retries):
        try:
            if verbose and attempt > 0:
                print(f"  Retry attempt {attempt + 1}/{max_retries}...")
            
            # Build request parameters
            # Max output tokens: Sonnet 4.5 = 16384, Opus 4.5 = 32768
            max_tokens = 32768 if model == MODEL_OPUS else 16384
            
            request_params = {
                "model": model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [
                    {"role": "user", "content": user_message}
                ]
            }
            
            # Add extended thinking if enabled
            if use_thinking:
                request_params["temperature"] = 1  # Required for extended thinking
                request_params["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": 50000
                }
                
                # Use streaming for thinking (required for long requests)
                response_text = ""
                thinking_text = ""
                
                with client.messages.stream(**request_params) as stream:
                    for event in stream:
                        pass  # Just consume the stream
                    
                    # Get final message from stream
                    response = stream.get_final_message()
                
                result.elapsed_seconds = time.time() - start_time
                result.input_tokens = response.usage.input_tokens
                result.output_tokens = response.usage.output_tokens
                
                # Get thinking tokens from usage
                if hasattr(response.usage, 'cache_read_input_tokens'):
                    # Extended thinking returns thinking tokens differently
                    pass
                # Check for thinking tokens in the response usage
                usage_dict = response.usage.model_dump() if hasattr(response.usage, 'model_dump') else {}
                result.thinking_tokens = usage_dict.get('thinking_tokens', 0) or 0
                
                # Extract text from response (skip thinking blocks)
                for block in response.content:
                    if block.type == "text":
                        response_text += block.text
                    elif block.type == "thinking":
                        thinking_text += block.thinking
                
                result.raw_response = response_text
                
            else:
                # Non-thinking: use regular request
                response = client.messages.create(**request_params)
                
                result.elapsed_seconds = time.time() - start_time
                result.input_tokens = response.usage.input_tokens
                result.output_tokens = response.usage.output_tokens
                
                # Extract text from response
                response_text = ""
                for block in response.content:
                    if block.type == "text":
                        response_text += block.text
                
                result.raw_response = response_text
            
            # Parse findings
            try:
                result.findings = parse_findings(response_text)
            except ValueError as e:
                result.error = f"Failed to parse response: {e}"
            
            return result
            
        except RateLimitError as e:
            last_error = e
            wait_time = 2 ** attempt * 10  # 10s, 20s, 40s
            if verbose:
                print(f"  Rate limited. Waiting {wait_time}s...")
            time.sleep(wait_time)
            
        except APIConnectionError as e:
            last_error = e
            wait_time = 2 ** attempt * 5  # 5s, 10s, 20s
            if verbose:
                print(f"  Connection error. Waiting {wait_time}s...")
            time.sleep(wait_time)
            
        except APIError as e:
            # Non-retryable API error
            result.error = f"API error: {e}"
            result.elapsed_seconds = time.time() - start_time
            return result
    
    # All retries exhausted
    result.error = f"Failed after {max_retries} attempts. Last error: {last_error}"
    result.elapsed_seconds = time.time() - start_time
    return result
