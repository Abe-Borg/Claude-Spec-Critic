"""
Claude API client for specification review.

Handles streaming responses, JSON parsing, retry logic, and token tracking.
Uses Claude Opus 4.6 exclusively.
"""
from __future__ import annotations

import os
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from anthropic import Anthropic, APIError, APIConnectionError, RateLimitError

from .prompts import get_system_prompt, get_user_message


MODEL_OPUS_46 = "claude-opus-4-6"

StreamCallback = Callable[[str], None]


@dataclass
class Finding:
    """A single review finding from Claude's analysis."""
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
    """Complete result of a specification review API call."""
    findings: list[Finding] = field(default_factory=list)
    raw_response: str = ""
    thinking: str = ""
    model: str = MODEL_OPUS_46
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
    """Retrieve API key from environment variable."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("ANTHROPIC_API_KEY environment variable not set")
    return key


def _extract_json_array(text: str) -> tuple[list, str]:
    """Extract the JSON findings array and analysis summary from Claude's response."""
    start_idx = text.find("[")
    end_idx = text.rfind("]")
    
    if start_idx == -1 or end_idx == -1:
        if "no issues" in text.lower() or text.strip() == "[]":
            return [], text.strip()
        raise ValueError(f"Could not find JSON array in response: {text[:200]}...")

    thinking = text[:start_idx].strip()
    
    json_str = text[start_idx:end_idx + 1]
    data = json.loads(json_str)
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array, got: {type(data)}")
    return data, thinking


def _parse_findings(data: list) -> list[Finding]:
    """Parse raw JSON dicts into Finding dataclass instances."""
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


def review_specs(
    combined_content: str,
    *,
    max_retries: int = 3,
    verbose: bool = False,
    stream_callback: Optional[StreamCallback] = None,
) -> ReviewResult:
    """
    Send specifications to Claude for review and parse the response.
    
    Uses streaming API for real-time display. Retries on rate limit
    and connection errors with exponential backoff.
    """
    start_time = time.time()
    result = ReviewResult(model=MODEL_OPUS_46)

    client = Anthropic(api_key=_get_api_key())

    system_prompt = get_system_prompt()
    user_message = get_user_message(combined_content)

    max_tokens = 32768

    for attempt in range(max_retries):
        try:
            if verbose:
                print(f"Calling Claude (attempt {attempt + 1}/{max_retries})...")

            with client.messages.stream(
                model=MODEL_OPUS_46,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            ) as stream:
                
                response_chunks: list[str] = []
                
                for text in stream.text_stream:
                    response_chunks.append(text)
                    
                    if stream_callback:
                        try:
                            stream_callback(text)
                        except Exception:
                            pass
                
                resp = stream.get_final_message()
            
            response_text = "".join(response_chunks)
            result.raw_response = response_text

            try:
                usage = getattr(resp, "usage", None)
                if usage:
                    result.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                    result.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            except Exception:
                pass
            
            data, thinking = _extract_json_array(response_text)
            result.findings = _parse_findings(data)
            result.thinking = thinking
            result.elapsed_seconds = time.time() - start_time
            return result

        except RateLimitError as e:
            wait_time = 2 ** attempt * 10
            if verbose:
                print(f"Rate limit: {e}. Waiting {wait_time}s...")
            time.sleep(wait_time)

        except APIConnectionError as e:
            wait_time = 2 ** attempt * 5
            if verbose:
                print(f"Connection error: {e}. Waiting {wait_time}s...")
            time.sleep(wait_time)

        except APIError as e:
            result.error = f"API error: {e}"
            result.elapsed_seconds = time.time() - start_time
            return result

        except Exception as e:
            result.error = f"Error: {e}"
            result.elapsed_seconds = time.time() - start_time
            return result

    result.error = f"Failed after {max_retries} attempts."
    result.elapsed_seconds = time.time() - start_time
    return result