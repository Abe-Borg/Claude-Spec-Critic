"""Cross-spec coordination checker for Spec Critic."""

from __future__ import annotations

import time
from typing import Callable

from anthropic import APIError, APIConnectionError, RateLimitError

from .extractor import ExtractedSpec
from .reviewer import Finding, ReviewResult, _extract_json_array, _parse_findings, _get_client, MODEL_OPUS_46
from .tokenizer import CROSS_CHECK_RECOMMENDED_MAX, count_tokens
from .code_cycles import CodeCycle, DEFAULT_CYCLE

StreamCallback = Callable[[str], None]


def _build_cross_check_input(specs: list[ExtractedSpec], existing_findings: list[Finding]) -> str:
    parts: list[str] = []
    for spec in specs:
        parts.append(f"\n===== FILE: {spec.filename} =====")
        parts.append(spec.content)
    if existing_findings:
        parts.append("\n" + "=" * 40)
        parts.append("ISSUES ALREADY IDENTIFIED (do NOT repeat)")
        for f in existing_findings:
            parts.append(f"[{f.severity}] {f.fileName} — {f.issue[:160]}")
    return "\n".join(parts)


def _cross_system_prompt(cycle: CodeCycle) -> str:
    return (
        "You are a cross-spec coordination reviewer for California K-12 DSA mechanical/plumbing specs. "
        f"Use current cycle CBC {cycle.cbc}, CMC {cycle.cmc}, CPC {cycle.cpc}, CALGreen {cycle.calgreen}, ASCE {cycle.asce7}. "
        "Find only BETWEEN-spec issues (contradictions, missing references, scope gaps/overlaps). "
        "Do not repeat already-identified per-spec issues. Output findings JSON in <FINDINGS_JSON> tags."
    )


def _get_cross_check_user_message(spec_input: str, file_count: int, project_context: str = "") -> str:
    ctx = f"\n<project_context>\n{project_context.strip()}\n</project_context>\n" if project_context.strip() else ""
    return f"Review the following {file_count} specs for cross-spec coordination only.\n{ctx}\n{spec_input}"


def run_cross_check(specs: list[ExtractedSpec], existing_findings: list[Finding], *, project_context: str = "", max_retries: int = 3, verbose: bool = False, stream_callback: StreamCallback | None = None, cycle: CodeCycle = DEFAULT_CYCLE) -> ReviewResult:
    if len(specs) < 2:
        return ReviewResult(findings=[], thinking="Need at least 2 specs.", model=MODEL_OPUS_46, cross_check_status="skipped")

    system_prompt = _cross_system_prompt(cycle)
    user_message = _get_cross_check_user_message(_build_cross_check_input(specs, existing_findings), len(specs), project_context=project_context)
    total_input_tokens = count_tokens(system_prompt) + count_tokens(user_message)
    if total_input_tokens > CROSS_CHECK_RECOMMENDED_MAX:
        return ReviewResult(findings=[], thinking=f"Combined input ({total_input_tokens:,}) exceeds cross-check limit ({CROSS_CHECK_RECOMMENDED_MAX:,}).", model=MODEL_OPUS_46, cross_check_status="skipped")

    client = _get_client()
    start = time.time()
    result = ReviewResult(model=MODEL_OPUS_46)

    for attempt in range(max_retries):
        try:
            with client.messages.stream(model=MODEL_OPUS_46, max_tokens=128_000, thinking={"type": "adaptive"}, system=system_prompt, messages=[{"role": "user", "content": user_message}]) as stream:
                chunks: list[str] = []
                for text in stream.text_stream:
                    chunks.append(text)
                    if stream_callback:
                        try: stream_callback(text)
                        except Exception: pass
                resp = stream.get_final_message()

            result.raw_response = "".join(chunks)
            result.stop_reason = getattr(resp, "stop_reason", None)
            usage = getattr(resp, "usage", None)
            if usage:
                result.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                result.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)

            if result.stop_reason != "end_turn":
                result.parse_status = "incomplete"
                result.error = f"Response incomplete (stop_reason: {result.stop_reason})."
                result.cross_check_status = "failed"
                result.elapsed_seconds = time.time() - start
                return result

            data, thinking = _extract_json_array(result.raw_response, stop_reason=result.stop_reason)
            result.findings = _parse_findings(data)
            result.thinking = thinking
            result.parse_status = "ok"
            result.cross_check_status = "completed"
            result.elapsed_seconds = time.time() - start
            return result
        except (RateLimitError, APIConnectionError):
            time.sleep(2 ** attempt * 5)
        except APIError as e:
            result.error = f"API error: {e}"
            result.cross_check_status = "failed"
            result.elapsed_seconds = time.time() - start
            return result
        except Exception as e:
            result.error = f"Error: {e}"
            result.parse_status = "parse_error"
            result.cross_check_status = "failed"
            result.elapsed_seconds = time.time() - start
            return result

    result.error = f"Failed after {max_retries} attempts."
    result.cross_check_status = "failed"
    result.elapsed_seconds = time.time() - start
    return result
