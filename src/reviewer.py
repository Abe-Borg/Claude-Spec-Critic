"""
Claude API client for specification review.

Single-model design:
- Always uses Opus 4.5 (no model selection in this repo).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from anthropic import Anthropic, APIError, APIConnectionError, RateLimitError

from .prompts import get_system_prompt, get_user_message


# Single allowed model for this repo
MODEL_OPUS_45 = "claude-opus-4-5-20251101"


@dataclass
class Finding:
    """A single review finding."""
    severity: str
    file: str
    location: str
    issue: str
    recommendation: str


@dataclass
class ReviewResult:
    """Result of a specification review."""
    findings: list[Finding] = field(default_factory=list)
    raw_response: str = ""
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


def get_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("ANTHROPIC_API_KEY environment variable not set")
    return key


def _extract_json_object(text: str) -> str:
    """
    Extract the first top-level JSON object from a response.
    Prompts demand JSON-only, but this makes us more resilient.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model response")
    return text[start:end + 1]


def _parse_findings(response_text: str) -> list[Finding]:
    obj_text = _extract_json_object(response_text)
    data = json.loads(obj_text)

    findings_raw = data.get("findings", [])
    findings: list[Finding] = []
    for f in findings_raw:
        if not isinstance(f, dict):
            continue
        # Best-effort field extraction
        severity = str(f.get("severity", "")).strip()
        file = str(f.get("file", "")).strip()
        location = str(f.get("location", "")).strip()
        issue = str(f.get("issue", "")).strip()
        recommendation = str(f.get("recommendation", "")).strip()

        if not severity or not issue:
            continue

        findings.append(
            Finding(
                severity=severity,
                file=file,
                location=location,
                issue=issue,
                recommendation=recommendation,
            )
        )
    return findings


def review_specs(
    combined_content: str,
    *,
    max_retries: int = 3,
    verbose: bool = False,
) -> ReviewResult:
    """
    Send specifications to Claude for review (Opus 4.5 only).
    """
    start_time = time.time()
    result = ReviewResult(model=MODEL_OPUS_45)

    client = Anthropic(api_key=get_api_key())

    system_prompt = get_system_prompt()
    user_message = get_user_message(combined_content)

    # Tune this if you want shorter/longer outputs
    max_tokens = 32768

    last_error: str | None = None

    for attempt in range(max_retries):
        try:
            if verbose:
                print(f"Calling Claude (attempt {attempt + 1}/{max_retries})...")

            resp = client.messages.create(
                model=MODEL_OPUS_45,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )

            # Anthropic SDK response format: content is a list of blocks.
            response_text = ""
            try:
                # Typical: resp.content[0].text
                if resp.content and hasattr(resp.content[0], "text"):
                    response_text = resp.content[0].text or ""
                else:
                    response_text = str(resp.content)
            except Exception:
                response_text = str(resp)

            result.raw_response = response_text

            # Token usage (best-effort; varies by SDK version)
            try:
                usage = getattr(resp, "usage", None)
                if usage:
                    result.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                    result.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            except Exception:
                pass

            # Parse JSON findings
            result.findings = _parse_findings(response_text)

            result.elapsed_seconds = time.time() - start_time
            return result

        except RateLimitError as e:
            last_error = f"Rate limit: {e}"
            wait_time = 2 ** attempt * 10  # 10s, 20s, 40s...
            if verbose:
                print(f"  {last_error}. Waiting {wait_time}s...")
            time.sleep(wait_time)

        except APIConnectionError as e:
            last_error = f"Connection error: {e}"
            wait_time = 2 ** attempt * 5  # 5s, 10s, 20s...
            if verbose:
                print(f"  {last_error}. Waiting {wait_time}s...")
            time.sleep(wait_time)

        except APIError as e:
            result.error = f"API error: {e}"
            result.elapsed_seconds = time.time() - start_time
            return result

        except Exception as e:
            # Parsing or unexpected failures
            result.error = f"Error: {e}"
            result.elapsed_seconds = time.time() - start_time
            return result

    result.error = f"Failed after {max_retries} attempts. Last error: {last_error}"
    result.elapsed_seconds = time.time() - start_time
    return result
