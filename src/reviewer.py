"""Claude API client for specification review."""
from __future__ import annotations

import os
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .verifier import VerificationResult

from anthropic import Anthropic, APIError, APIConnectionError, RateLimitError

from .prompts import get_system_prompt, get_single_spec_user_message
from .code_cycles import CodeCycle, DEFAULT_CYCLE
from .tokenizer import MAX_OUTPUT_TOKENS_OPUS, MAX_OUTPUT_TOKENS_SONNET

MODEL_OPUS_46 = "claude-opus-4-6"
REVIEW_MODELS = {"Opus 4.6": MODEL_OPUS_46}
StreamCallback = Callable[[str], None]


@dataclass
class Finding:
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
    affected_files: list[str] = field(default_factory=list)


@dataclass
class ReviewResult:
    findings: list[Finding] = field(default_factory=list)
    raw_response: str = ""
    thinking: str = ""
    model: str = MODEL_OPUS_46
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_seconds: float = 0.0
    error: str | None = None
    stop_reason: str | None = None
    parse_status: str | None = None
    cross_check_status: str | None = None

    @property
    def critical_count(self) -> int: return sum(1 for f in self.findings if f.severity == "CRITICAL")
    @property
    def high_count(self) -> int: return sum(1 for f in self.findings if f.severity == "HIGH")
    @property
    def medium_count(self) -> int: return sum(1 for f in self.findings if f.severity == "MEDIUM")
    @property
    def gripe_count(self) -> int: return sum(1 for f in self.findings if f.severity == "GRIPES")
    @property
    def total_count(self) -> int: return len(self.findings)


def _get_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("ANTHROPIC_API_KEY environment variable not set")
    return key


_cached_client: Anthropic | None = None
_cached_key: str | None = None


def _get_client() -> Anthropic:
    global _cached_client, _cached_key
    key = _get_api_key()
    if _cached_client is None or _cached_key != key:
        _cached_client = Anthropic(api_key=key)
        _cached_key = key
    return _cached_client


def _extract_json_array(text: str, *, stop_reason: str | None = None) -> tuple[list, str]:
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
        try:
            data = json.loads(json_str)
            if isinstance(data, list):
                return data, thinking
        except json.JSONDecodeError:
            pass

    if text.strip() == "[]":
        return [], text.strip()

    raise ValueError(f"Could not extract JSON findings from response (stop_reason: {stop_reason}): {text[:200]}...")


def _parse_findings(data: list) -> list[Finding]:
    findings: list[Finding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        sev = str(item.get("severity", "")).strip().upper()
        if sev not in {"CRITICAL", "HIGH", "MEDIUM", "GRIPES"}:
            continue
        action = str(item.get("actionType", "EDIT")).strip().upper()
        if action not in {"ADD", "EDIT", "DELETE"}:
            action = "EDIT"
        issue = str(item.get("issue") or "").strip()
        if not issue:
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
        except Exception:
            confidence = 0.5
        findings.append(Finding(
            severity=sev,
            fileName=str(item.get("fileName") or "").strip(),
            section=str(item.get("section") or "").strip(),
            issue=issue,
            actionType=action,
            existingText=str(item.get("existingText")) if item.get("existingText") is not None else None,
            replacementText=str(item.get("replacementText")) if item.get("replacementText") is not None else None,
            codeReference=str(item.get("codeReference")) if item.get("codeReference") is not None else None,
            confidence=confidence,
        ))
    return findings


def _stream_review(client: Anthropic, system_prompt: str, user_message: str, *, model: str = MODEL_OPUS_46, max_retries: int = 3, verbose: bool = False, stream_callback: Optional[StreamCallback] = None) -> ReviewResult:
    start_time = time.time()
    result = ReviewResult(model=model)
    output_limit = MAX_OUTPUT_TOKENS_OPUS if model == MODEL_OPUS_46 else MAX_OUTPUT_TOKENS_SONNET
    for attempt in range(max_retries):
        try:
            if verbose:
                print(f"Calling Claude {model} (attempt {attempt + 1}/{max_retries})...")
            with client.messages.stream(model=model, max_tokens=output_limit, system=system_prompt, messages=[{"role": "user", "content": user_message}]) as stream:
                chunks: list[str] = []
                for text in stream.text_stream:
                    chunks.append(text)
                    if stream_callback:
                        try: stream_callback(text)
                        except Exception: pass
                resp = stream.get_final_message()
            response_text = "".join(chunks)
            result.raw_response = response_text
            result.stop_reason = getattr(resp, "stop_reason", None)
            usage = getattr(resp, "usage", None)
            if usage:
                result.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                result.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)

            if result.stop_reason != "end_turn":
                result.parse_status = "incomplete"
                result.error = f"Response incomplete (stop_reason: {result.stop_reason}). The model likely ran out of output tokens. Partial response preserved in raw_response."
                result.elapsed_seconds = time.time() - start_time
                return result

            data, thinking = _extract_json_array(response_text, stop_reason=result.stop_reason)
            result.findings = _parse_findings(data)
            result.thinking = thinking
            result.parse_status = "ok"
            result.elapsed_seconds = time.time() - start_time
            return result
        except (RateLimitError, APIConnectionError):
            time.sleep(2 ** attempt * 5)
        except APIError as e:
            result.error = f"API error: {e}"
            result.elapsed_seconds = time.time() - start_time
            return result
        except Exception as e:
            result.error = f"Error: {e}"
            result.parse_status = "parse_error"
            result.elapsed_seconds = time.time() - start_time
            return result
    result.error = f"Failed after {max_retries} attempts."
    result.elapsed_seconds = time.time() - start_time
    return result


def review_single_spec(spec_content: str, filename: str, *, project_context: str = "", model: str = MODEL_OPUS_46, max_retries: int = 3, verbose: bool = False, stream_callback: Optional[StreamCallback] = None, cycle: CodeCycle = DEFAULT_CYCLE) -> ReviewResult:
    client = _get_client()
    return _stream_review(
        client,
        get_system_prompt(cycle),
        get_single_spec_user_message(spec_content, filename, project_context=project_context, cycle=cycle),
        model=model,
        max_retries=max_retries,
        verbose=verbose,
        stream_callback=stream_callback,
    )
