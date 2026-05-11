"""Claude API client for specification review."""
from __future__ import annotations

import os
import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .verifier import VerificationResult

from anthropic import Anthropic, APIError, APIConnectionError, APIStatusError, RateLimitError, InternalServerError

from .prompts import get_system_prompt, get_single_spec_user_message
from .code_cycles import CodeCycle, DEFAULT_CYCLE
from .review_modes import DEFAULT_REVIEW_MODE, ReviewMode
from .api_config import (
    MODEL_OPUS_46,
    MODEL_OPUS_47,
    PHASE_REVIEW,
    REVIEW_MODEL_DEFAULT,
    apply_thinking_config,
    extract_cache_usage,
    review_max_tokens,
    system_prompt_with_cache,
)
from .structured_schemas import (
    REVIEW_TOOL_NAME,
    extract_tool_use_block,
    review_findings_tool,
    review_tool_choice,
    structured_outputs_enabled,
)

REVIEW_MODELS = {"Opus 4.7": MODEL_OPUS_47}
StreamCallback = Callable[[str], None]

# ---------------------------------------------------------------------------
# Retryable connection-failure heuristic
# ---------------------------------------------------------------------------
# These patterns catch httpx / urllib3 / aiohttp transport-level failures
# that surface as generic Exception (not wrapped in anthropic APIError).
# They are transient and safe to retry.
_RETRYABLE_EXCEPTION_PATTERNS = (
    "peer closed connection",
    "incomplete chunked read",
    "connection reset",
    "connection closed",
    "timed out",
    "timeout",
    "broken pipe",
    "remotedisconnected",
    "connectionreset",
    "server disconnected",
    "eof occurred",
    "incomplete read",
)


def _is_retryable_connection_error(exc: Exception) -> bool:
    """Return True if a generic exception looks like a transient connection failure."""
    msg = str(exc).lower()
    return any(pattern in msg for pattern in _RETRYABLE_EXCEPTION_PATTERNS)


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
    # ADD-action insertion model (audit Issue 5). When the model explicitly
    # provides an anchor and a side, the editor inserts deterministically
    # instead of falling back to brittle prefix/suffix text heuristics.
    anchorText: str | None = None
    insertPosition: str | None = None  # "before" | "after" | None


@dataclass
class ReviewResult:
    findings: list[Finding] = field(default_factory=list)
    raw_response: str = ""
    thinking: str = ""
    model: str = MODEL_OPUS_47
    input_tokens: int = 0
    output_tokens: int = 0
    # Phase 2 prompt-caching telemetry. Populated when the API returns
    # cache_creation_input_tokens / cache_read_input_tokens in usage.
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
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
    """Fallback parser for the legacy ``<findings_json>``-tagged text path.

    Phase 2.4 (audit Section 6.4) replaces this with structured tool-use
    outputs as the primary path. This function remains as a fallback when
    the model returns no tool_use block (e.g., refusal or feature flag off).
    """
    tagged = re.search(r"<\s*findings_json\s*>(.*?)<\s*/\s*findings_json\s*>", text, flags=re.IGNORECASE | re.DOTALL)
    if tagged:
        json_str = tagged.group(1).strip()
        thinking = text[:tagged.start()].strip()
        try:
            data = json.loads(json_str)
            if (
                isinstance(data, list)
                and all(isinstance(item, dict) for item in data)
                and all(("severity" in item and "issue" in item) for item in data)
            ):
                return data, thinking
        except json.JSONDecodeError:
            pass

    end_idx = text.rfind("]")
    while end_idx != -1:
        start_idx = text.rfind("[", 0, end_idx + 1)
        if start_idx == -1:
            break
        json_str = text[start_idx:end_idx + 1]
        thinking = text[:start_idx].strip()
        try:
            data = json.loads(json_str)
            if (
                isinstance(data, list)
                and all(isinstance(item, dict) for item in data)
                and all(("severity" in item and "issue" in item) for item in data)
            ):
                return data, thinking
        except json.JSONDecodeError:
            pass
        end_idx = text.rfind("]", 0, end_idx)

    if text.strip() == "[]":
        return [], text.strip()

    raise ValueError(f"Could not extract JSON findings from response (stop_reason: {stop_reason}): {text[:200]}...")


def _extract_structured_findings(resp) -> tuple[list[dict], str] | None:
    """Pull findings out of a tool_use block when structured outputs are used.

    Returns ``(findings_list, analysis_summary)`` if a matching tool_use
    block is present, else None — callers fall back to text parsing.
    """
    payload = extract_tool_use_block(resp, REVIEW_TOOL_NAME)
    if not isinstance(payload, dict):
        return None
    findings = payload.get("findings") or []
    if not isinstance(findings, list):
        findings = []
    summary = str(payload.get("analysis_summary") or "")
    return findings, summary


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
        anchor_raw = item.get("anchorText")
        anchor_text = str(anchor_raw).strip() if anchor_raw is not None else None
        if anchor_text == "":
            anchor_text = None
        position_raw = item.get("insertPosition")
        position = str(position_raw).strip().lower() if position_raw is not None else None
        if position not in {"before", "after"}:
            position = None
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
            anchorText=anchor_text,
            insertPosition=position,
        ))
    return findings


def _stream_review(client: Anthropic, system_prompt: str, user_message: str, *, model: str = MODEL_OPUS_47, max_retries: int = 3, verbose: bool = False, stream_callback: Optional[StreamCallback] = None) -> ReviewResult:
    start_time = time.time()
    result = ReviewResult(model=model)
    # Per-call output cap. Real-time and batch share the same baseline so
    # findings cannot diverge between modes; the 300k extended path is a
    # batch-only API capability (300k beta header is not honored on stream).
    output_limit = review_max_tokens(model=model)
    # Chunk J: phase-aware cache policy. Real-time review uses the
    # PHASE_REVIEW policy (cache=on, ttl=1h). Routing through the phase
    # parameter keeps the policy decision in api_config so a future
    # tuning pass touches one place.
    system_payload = system_prompt_with_cache(system_prompt, phase=PHASE_REVIEW)
    # Phase 2.4: when structured outputs are enabled, force the model to
    # emit a tool_use block whose ``input`` matches the finding schema.
    # ``tool_choice`` removes the "did the model wrap its output in tags?"
    # parse-failure mode entirely.
    use_structured = structured_outputs_enabled()
    request_kwargs: dict = {
        "model": model,
        "max_tokens": output_limit,
        "system": system_payload,
        "messages": [{"role": "user", "content": user_message}],
    }
    apply_thinking_config(request_kwargs, model=model, phase=PHASE_REVIEW)
    if use_structured:
        request_kwargs["tools"] = [review_findings_tool()]
        request_kwargs["tool_choice"] = review_tool_choice()
    last_exception: Exception | None = None
    for attempt in range(max_retries):
        is_last_attempt = attempt == max_retries - 1
        try:
            if verbose:
                print(f"Calling Claude {model} (attempt {attempt + 1}/{max_retries})...")
            with client.messages.stream(**request_kwargs) as stream:
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
                cache = extract_cache_usage(usage)
                result.cache_creation_input_tokens = cache["cache_creation_input_tokens"]
                result.cache_read_input_tokens = cache["cache_read_input_tokens"]

            # Tool-use stops report stop_reason="tool_use", which is the
            # success path when structured outputs are forced.
            if result.stop_reason not in ("end_turn", "tool_use"):
                result.parse_status = "incomplete"
                result.error = f"Response incomplete (stop_reason: {result.stop_reason}). The model likely ran out of output tokens. Partial response preserved in raw_response."
                result.elapsed_seconds = time.time() - start_time
                return result

            structured = _extract_structured_findings(resp) if use_structured else None
            if structured is not None:
                data, thinking = structured
            else:
                data, thinking = _extract_json_array(response_text, stop_reason=result.stop_reason)
            result.findings = _parse_findings(data)
            result.thinking = thinking
            result.parse_status = "ok"
            result.elapsed_seconds = time.time() - start_time
            return result
        except (RateLimitError, APIConnectionError) as e:
            last_exception = e
            if is_last_attempt:
                break
            time.sleep(2 ** attempt * 5)
        except InternalServerError as e:
            last_exception = e
            if is_last_attempt:
                break
            time.sleep(2 ** attempt * 10)
        except APIStatusError as e:
            if getattr(e, "status_code", None) == 529 or e.__class__.__name__ == "OverloadedError":
                last_exception = e
                if is_last_attempt:
                    break
                time.sleep(2 ** attempt * 10)
                continue
            result.error = f"API error: {e}"
            result.elapsed_seconds = time.time() - start_time
            return result
        except APIError as e:
            result.error = f"API error: {e}"
            result.elapsed_seconds = time.time() - start_time
            return result
        except Exception as e:
            # Retry transient connection failures, but don't sleep after the
            # final attempt — and surface the underlying exception detail
            # rather than a generic "failed after N attempts" (audit Issue 9).
            if _is_retryable_connection_error(e) and not is_last_attempt:
                backoff = 2 ** attempt * 5
                last_exception = e
                if verbose:
                    print(f"Retryable connection error (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {backoff}s...")
                time.sleep(backoff)
                continue
            result.error = f"Error: {e}"
            result.parse_status = "parse_error"
            result.elapsed_seconds = time.time() - start_time
            return result
    if last_exception is not None:
        result.error = (
            f"Failed after {max_retries} attempts: "
            f"{type(last_exception).__name__}: {last_exception}"
        )
    else:
        result.error = f"Failed after {max_retries} attempts."
    result.elapsed_seconds = time.time() - start_time
    return result


def review_single_spec(spec_content: str, filename: str, *, project_context: str = "", model: str = REVIEW_MODEL_DEFAULT, max_retries: int = 3, verbose: bool = False, stream_callback: Optional[StreamCallback] = None, cycle: CodeCycle = DEFAULT_CYCLE, mode: ReviewMode = DEFAULT_REVIEW_MODE) -> ReviewResult:
    client = _get_client()
    return _stream_review(
        client,
        get_system_prompt(cycle, mode=mode),
        get_single_spec_user_message(spec_content, filename, project_context=project_context, cycle=cycle, mode=mode),
        model=model,
        max_retries=max_retries,
        verbose=verbose,
        stream_callback=stream_callback,
    )