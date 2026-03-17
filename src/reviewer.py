"""
Claude API client for specification review.

Handles streaming responses, JSON parsing, retry logic, and token tracking.

v2.3.0 — Opus-only across the entire pipeline. All review, verification,
    and cross-spec coordination uses Claude Opus 4.6.

v1.5.0 — Added confidence field (0.0-1.0) to Finding dataclass.

v1.4.0 — Added review_single_spec() for per-spec siloed review.
"""
from __future__ import annotations

import os
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .verifier import VerificationResult

from anthropic import Anthropic, APIError, APIConnectionError, RateLimitError

from .prompts import get_system_prompt, get_single_spec_user_message


MODEL_OPUS_46 = "claude-opus-4-6"

# Available models for review (v2.3.0: Opus only)
REVIEW_MODELS = {
    "Opus 4.6": MODEL_OPUS_46,
}

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
    confidence: float = 0.5
    verification: VerificationResult | None = None


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
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("ANTHROPIC_API_KEY environment variable not set")
    return key


def _extract_json_array(text: str) -> tuple[list, str]:
    """Extract the JSON findings array and analysis summary from Claude's response."""
    tag_start = text.find("<FINDINGS_JSON>")
    tag_end = text.find("</FINDINGS_JSON>")
    if tag_start != -1 and tag_end != -1 and tag_end > tag_start:
        thinking = text[:tag_start].strip()
        json_str = text[tag_start + len("<FINDINGS_JSON>"):tag_end].strip()
        try:
            data = json.loads(json_str)
            if isinstance(data, list):
                return data, thinking
        except json.JSONDecodeError:
            pass

    start_idx = text.find("[")
    end_idx = text.rfind("]")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        thinking = text[:start_idx].strip()
        json_str = text[start_idx:end_idx + 1]
        stripped = json_str.lstrip()
        if stripped.startswith("[{") or stripped.strip() == "[]":
            try:
                data = json.loads(json_str)
                if isinstance(data, list):
                    return data, thinking
            except json.JSONDecodeError:
                pass

    if text.strip() == "[]":
        return [], text.strip()

    raise ValueError(f"Could not extract JSON findings from response: {text[:200]}...")


def _parse_findings(data: list) -> list[Finding]:
    """Parse raw JSON dicts into Finding dataclass instances."""
    findings: list[Finding] = []
    for item in data:
        if not isinstance(item, dict):
            continue

        raw_confidence = item.get("confidence")
        if raw_confidence is not None:
            try:
                confidence = max(0.0, min(1.0, float(raw_confidence)))
            except (TypeError, ValueError):
                confidence = 0.5
        else:
            confidence = 0.5

        severity = str(item.get("severity", "")).strip().upper()
        if severity not in {"CRITICAL", "HIGH", "MEDIUM", "GRIPES"}:
            continue

        action_type = str(item.get("actionType", "")).strip().upper()
        if action_type not in {"ADD", "EDIT", "DELETE"}:
            action_type = "EDIT"

        existing = item.get("existingText")
        existing_text = str(existing) if existing is not None else None
        replacement = item.get("replacementText")
        replacement_text = str(replacement) if replacement is not None else None
        code_ref = item.get("codeReference")
        code_reference = str(code_ref) if code_ref is not None else None

        raw_fn = item.get("fileName")
        file_name = str(raw_fn).strip() if raw_fn is not None else ""
        raw_sec = item.get("section")
        section = str(raw_sec).strip() if raw_sec is not None else ""
        raw_issue = item.get("issue")
        issue_text = str(raw_issue).strip() if raw_issue is not None else ""

        if not issue_text:
            continue

        findings.append(
            Finding(
                severity=severity,
                fileName=file_name,
                section=section,
                issue=issue_text,
                actionType=action_type,
                existingText=existing_text,
                replacementText=replacement_text,
                codeReference=code_reference,
                confidence=confidence,
            )
        )
    return findings


def _stream_review(
    client: Anthropic,
    system_prompt: str,
    user_message: str,
    *,
    model: str = MODEL_OPUS_46,
    max_retries: int = 3,
    verbose: bool = False,
    stream_callback: Optional[StreamCallback] = None,
) -> ReviewResult:
    """Core streaming review logic."""
    start_time = time.time()
    result = ReviewResult(model=model)
    max_tokens = 32768

    for attempt in range(max_retries):
        try:
            if verbose:
                print(f"Calling Claude {model} (attempt {attempt + 1}/{max_retries})...")

            with client.messages.stream(
                model=model,
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


def review_single_spec(
    spec_content: str,
    filename: str,
    *,
    project_context: str = "",
    model: str = MODEL_OPUS_46,
    max_retries: int = 3,
    verbose: bool = False,
    stream_callback: Optional[StreamCallback] = None,
) -> ReviewResult:
    """Review a single spec file via streaming API call (per-spec siloed mode)."""
    client = Anthropic(api_key=_get_api_key())
    system_prompt = get_system_prompt()
    user_message = get_single_spec_user_message(
        spec_content,
        filename,
        project_context=project_context,
    )

    return _stream_review(
        client,
        system_prompt,
        user_message,
        model=model,
        max_retries=max_retries,
        verbose=verbose,
        stream_callback=stream_callback,
    )