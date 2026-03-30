"""Cross-spec coordination checker for Spec Critic."""

from __future__ import annotations

import time
from typing import Callable

from anthropic import APIError, APIConnectionError, APIStatusError, RateLimitError, InternalServerError

from .extractor import ExtractedSpec
from .reviewer import Finding, ReviewResult, _extract_json_array, _parse_findings, _get_client, MODEL_OPUS_46
from .tokenizer import CROSS_CHECK_RECOMMENDED_MAX, count_tokens
from .code_cycles import CodeCycle, DEFAULT_CYCLE

StreamCallback = Callable[[str], None]


def _sanitize_narrative(text: str) -> str:
    """Strip markdown formatting artifacts from narrative text.

    The cross-check prompt explicitly requests plain text, but models
    sometimes emit markdown headers or formatting anyway. This strips
    common markdown artifacts so the text renders cleanly in Word and GUI.
    """
    if not text:
        return text
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        # Strip markdown headers: "## HEADING" -> "HEADING"
        stripped = line
        while stripped.startswith('#'):
            stripped = stripped[1:]
        stripped = stripped.strip()
        # Skip lines that were ONLY a markdown header with no content after stripping
        # (e.g., "##" by itself). Keep lines that had content after the #s.
        if line.startswith('#') and not stripped:
            continue
        cleaned.append(stripped if line.startswith('#') else line)
    return '\n'.join(cleaned)


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
        "You are a cross-spec coordination reviewer for California K-12 DSA mechanical/plumbing specs.\n\n"
        f"Current cycle: CBC {cycle.cbc}, CMC {cycle.cmc}, CPC {cycle.cpc}, "
        f"CALGreen {cycle.calgreen}, ASCE {cycle.asce7}.\n\n"
        "<task>\n"
        "Determine whether these specs are well-coordinated with each other. Your job is to evaluate "
        "cross-spec coordination quality — the answer may be that coordination is adequate.\n\n"
        "If genuine coordination problems exist between specs, report them. The types of issues that "
        "qualify are: contradictions between specs, missing cross-references, scope gaps or overlaps, "
        "inconsistent equipment data, and division-of-work conflicts.\n\n"
        "Do NOT repeat issues already identified in the per-spec review (listed at the end of the input).\n"
        "Do NOT report issues that exist entirely within a single spec.\n"
        "Return exactly as many findings as genuinely exist, including zero.\n"
        "</task>\n\n"
        "<severity_definitions>\n"
        "CRITICAL — showstoppers: direct contradictions between specs that would cause construction conflicts or DSA rejection.\n"
        "HIGH — major coordination gaps requiring correction before issuing.\n"
        "MEDIUM — meaningful cross-reference or consistency issues with moderate impact.\n"
        "GRIPES — minor coordination polish items.\n"
        "</severity_definitions>\n\n"
        "<output_format>\n"
        "First provide a COORDINATION SUMMARY, then wrap findings JSON "
        "in <FINDINGS_JSON>...</FINDINGS_JSON> tags.\n\n"
        "COORDINATION SUMMARY requirements:\n"
        "- Organize by coordination theme (e.g., 'Seismic Scope Overlap', "
        "'Equipment Cross-Reference Gaps', 'TAB Coordination Issues').\n"
        "- Write one paragraph per theme. Each paragraph should name the specific "
        "specs involved (by CSI number and short title), describe the conflict or gap, "
        "and state the practical consequence.\n"
        "- Use plain text only. Do NOT use markdown headers (##), bullet points, "
        "bold (**), or any other markdown formatting. The summary is rendered in "
        "contexts that do not support markdown.\n"
        "- Separate paragraphs with a blank line.\n"
        "- Cover every coordination theme represented in your findings. If no issues were found, "
        "write a brief summary stating that cross-spec coordination appears adequate and note "
        "any areas where coordination is particularly well-handled.\n\n"
        "Each finding must be a JSON object with these fields:\n"
        '- severity: "CRITICAL" | "HIGH" | "MEDIUM" | "GRIPES"\n'
        "- fileName: primary file where the issue is most visible\n"
        "- section: section reference\n"
        "- issue: describe the cross-spec conflict (mention both files involved)\n"
        '- actionType: "ADD" | "EDIT" | "DELETE"\n'
        "- existingText: the problematic text (from the primary file)\n"
        "- replacementText: suggested correction\n"
        "- codeReference: applicable code or standard\n"
        "- confidence: 0.0-1.0\n\n"
        "If no cross-spec issues are found, return an empty array:\n"
        "<FINDINGS_JSON>\n[]\n</FINDINGS_JSON>\n\n"
        "CRITICAL: You MUST wrap the JSON array in <FINDINGS_JSON> tags. "
        "Do NOT output findings as markdown, bullet points, or prose. "
        "The JSON array is machine-parsed and will fail if not properly tagged.\n"
        "</output_format>"
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
            thinking = _sanitize_narrative(thinking)
            result.findings = _parse_findings(data)
            result.thinking = thinking
            result.parse_status = "ok"
            result.cross_check_status = "completed"
            result.elapsed_seconds = time.time() - start
            return result
        except (RateLimitError, APIConnectionError):
            time.sleep(2 ** attempt * 5)
        except InternalServerError:
            time.sleep(2 ** attempt * 10)
        except APIStatusError as e:
            if getattr(e, "status_code", None) == 529 or e.__class__.__name__ == "OverloadedError":
                time.sleep(2 ** attempt * 10)
                continue
            result.error = f"API error: {e}"
            result.cross_check_status = "failed"
            result.elapsed_seconds = time.time() - start
            return result
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
