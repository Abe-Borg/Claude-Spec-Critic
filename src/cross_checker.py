"""Cross-spec coordination checker for Spec Critic."""

from __future__ import annotations

import re
import time
from typing import Callable

from anthropic import APIError, APIConnectionError, APIStatusError, RateLimitError, InternalServerError

from .extractor import ExtractedSpec
from .reviewer import Finding, ReviewResult, _extract_json_array, _parse_findings, _get_client, MODEL_OPUS_46
from .tokenizer import CROSS_CHECK_RECOMMENDED_MAX, count_tokens
from .code_cycles import CodeCycle, DEFAULT_CYCLE
from .api_config import (
    CROSS_CHECK_MODEL_DEFAULT,
    cross_check_max_tokens,
    extract_cache_usage,
    system_prompt_with_cache,
)
from .structured_schemas import (
    CROSS_CHECK_TOOL_NAME,
    cross_check_findings_tool,
    cross_check_tool_choice,
    extract_tool_use_block,
    structured_outputs_enabled,
)

StreamCallback = Callable[[str], None]
LogFn = Callable[..., None]


def _noop_log(_msg: str, **_kwargs: object) -> None:
    return


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
        "Submit your review by calling the ``submit_cross_check_findings`` "
        "tool exactly once. Provide a plain-text ``coordination_summary`` and "
        "a ``findings`` array (zero or more items).\n\n"
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
        "Each finding object has these fields:\n"
        '- severity: "CRITICAL" | "HIGH" | "MEDIUM" | "GRIPES"\n'
        "- fileName: primary file where the issue is most visible\n"
        "- section: section reference\n"
        "- issue: describe the cross-spec conflict (mention both files involved)\n"
        '- actionType: "ADD" | "EDIT" | "DELETE"\n'
        "- existingText: the problematic text (from the primary file)\n"
        "- replacementText: suggested correction\n"
        "- codeReference: applicable code or standard\n"
        "- confidence: 0.0-1.0\n\n"
        "If no cross-spec issues are found, call the tool with an empty "
        "``findings`` array.\n\n"
        "Compatibility fallback: when the tool is unavailable, emit findings "
        "as a JSON array wrapped in ``<FINDINGS_JSON>...</FINDINGS_JSON>``.\n"
        "</output_format>"
    )


def _get_cross_check_user_message(spec_input: str, file_count: int, project_context: str = "") -> str:
    ctx = f"\n<project_context>\n{project_context.strip()}\n</project_context>\n" if project_context.strip() else ""
    return f"Review the following {file_count} specs for cross-spec coordination only.\n{ctx}\n{spec_input}"


def run_cross_check(specs: list[ExtractedSpec], existing_findings: list[Finding], *, project_context: str = "", max_retries: int = 3, verbose: bool = False, stream_callback: StreamCallback | None = None, cycle: CodeCycle = DEFAULT_CYCLE, model: str = CROSS_CHECK_MODEL_DEFAULT) -> ReviewResult:
    if len(specs) < 2:
        return ReviewResult(findings=[], thinking="Need at least 2 specs.", model=model, cross_check_status="skipped")

    system_prompt = _cross_system_prompt(cycle)
    user_message = _get_cross_check_user_message(_build_cross_check_input(specs, existing_findings), len(specs), project_context=project_context)
    total_input_tokens = count_tokens(system_prompt) + count_tokens(user_message)
    if total_input_tokens > CROSS_CHECK_RECOMMENDED_MAX:
        return ReviewResult(findings=[], thinking=f"Combined input ({total_input_tokens:,}) exceeds cross-check limit ({CROSS_CHECK_RECOMMENDED_MAX:,}).", model=model, cross_check_status="skipped")

    client = _get_client()
    start = time.time()
    result = ReviewResult(model=model)
    output_limit = cross_check_max_tokens(model=model)
    system_payload = system_prompt_with_cache(system_prompt)
    use_structured = structured_outputs_enabled()
    request_kwargs: dict = {
        "model": model,
        "max_tokens": output_limit,
        "thinking": {"type": "adaptive"},
        "system": system_payload,
        "messages": [{"role": "user", "content": user_message}],
    }
    if use_structured:
        request_kwargs["tools"] = [cross_check_findings_tool()]
        request_kwargs["tool_choice"] = cross_check_tool_choice()

    for attempt in range(max_retries):
        try:
            with client.messages.stream(**request_kwargs) as stream:
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
                cache = extract_cache_usage(usage)
                result.cache_creation_input_tokens = cache["cache_creation_input_tokens"]
                result.cache_read_input_tokens = cache["cache_read_input_tokens"]

            if result.stop_reason not in ("end_turn", "tool_use"):
                result.parse_status = "incomplete"
                result.error = f"Response incomplete (stop_reason: {result.stop_reason})."
                result.cross_check_status = "failed"
                result.elapsed_seconds = time.time() - start
                return result

            payload = extract_tool_use_block(resp, CROSS_CHECK_TOOL_NAME) if use_structured else None
            if isinstance(payload, dict):
                data = payload.get("findings") or []
                thinking = _sanitize_narrative(str(payload.get("coordination_summary") or ""))
            else:
                data, thinking = _extract_json_array(result.raw_response, stop_reason=result.stop_reason)
                thinking = _sanitize_narrative(thinking)
            if not isinstance(data, list):
                data = []
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


# ---------------------------------------------------------------------------
# Phase 8 / plan section 12.3: chunked cross-check for large projects
# ---------------------------------------------------------------------------

# CSI MasterFormat division 22/23 dominate K-12 mechanical/plumbing reviews.
# Chunking by these division families lets a 2,000-section megaproject still
# get coordination review instead of returning a "skipped" status when the
# combined input exceeds CROSS_CHECK_RECOMMENDED_MAX.
#
# The mapping is intentionally coarse — each chunk gets enough context to
# find within-discipline conflicts, and a final synthesis pass surfaces
# cross-discipline issues. Files whose CSI prefix does not match any chunk
# (rare) are pooled into a "general" chunk so they are never silently
# dropped.
_CSI_PREFIX_RE = re.compile(r"^\s*(\d{2})\s?(\d{2})?")


_CHUNK_GROUPS: list[tuple[str, str, frozenset[str]]] = [
    # (chunk_id, label, set of CSI division prefixes)
    ("div_21", "Division 21 — Fire Suppression", frozenset({"21"})),
    ("div_22", "Division 22 — Plumbing", frozenset({"22"})),
    ("div_23", "Division 23 — HVAC", frozenset({"23"})),
    # Division 25 controls + commissioning sections (often 23 09 / 25 xx /
    # 01 91 / 23 08 testing) live together so coordination claims about
    # sequences and TAB stay in one chunk.
    ("controls_commissioning", "Controls / Commissioning / TAB", frozenset({"25", "01"})),
]


def _csi_prefix(filename: str) -> str:
    match = _CSI_PREFIX_RE.match(filename)
    if not match:
        return ""
    return match.group(1) or ""


def _assign_chunk(filename: str) -> str:
    prefix = _csi_prefix(filename)
    if prefix:
        for chunk_id, _label, prefixes in _CHUNK_GROUPS:
            if prefix in prefixes:
                return chunk_id
    return "general"


def _chunk_label(chunk_id: str) -> str:
    for cid, label, _ in _CHUNK_GROUPS:
        if cid == chunk_id:
            return label
    return "Project-wide / Other"


def _group_specs_by_chunk(specs: list[ExtractedSpec]) -> list[tuple[str, list[ExtractedSpec]]]:
    """Group specs by CSI division-family chunk, preserving order.

    Returns a list of ``(chunk_id, specs)`` pairs with at least two specs
    per chunk; smaller chunks are merged into ``"general"`` so the chunked
    pass still has cross-spec context to work with.
    """
    buckets: dict[str, list[ExtractedSpec]] = {}
    for spec in specs:
        cid = _assign_chunk(spec.filename)
        buckets.setdefault(cid, []).append(spec)

    # Merge singletons into the project-wide bucket so each chunk has at
    # least two specs to coordinate against.
    merged: dict[str, list[ExtractedSpec]] = {}
    project_wide: list[ExtractedSpec] = []
    for cid, group in buckets.items():
        if len(group) >= 2:
            merged[cid] = group
        else:
            project_wide.extend(group)
    if project_wide:
        merged.setdefault("general", []).extend(project_wide)

    # Stable order: predefined chunk groups first, then "general" last.
    ordered: list[tuple[str, list[ExtractedSpec]]] = []
    for cid, _label, _ in _CHUNK_GROUPS:
        if cid in merged:
            ordered.append((cid, merged[cid]))
    if "general" in merged:
        ordered.append(("general", merged["general"]))
    # Anything else (shouldn't happen, but be defensive) preserves insertion order.
    for cid, group in merged.items():
        if cid not in {c for c, _ in ordered}:
            ordered.append((cid, group))
    return ordered


def _filter_findings_for_chunk(
    existing_findings: list[Finding], chunk_filenames: set[str]
) -> list[Finding]:
    """Restrict the "already-identified" context to findings inside a chunk.

    Per-spec review findings are noisy when shown to a chunk that does not
    contain the source file. Chunked cross-check sees only the findings
    that originate inside its files; the synthesis pass below sees the
    chunk-level coordination findings, not the per-spec ones, so it can
    focus on cross-discipline conflicts.
    """
    if not chunk_filenames:
        return list(existing_findings)
    return [
        f for f in existing_findings
        if f.fileName in chunk_filenames
        or any(name in chunk_filenames for name in f.affected_files)
    ]


def _label_finding_with_chunk(finding: Finding, chunk_id: str) -> Finding:
    """Tag a finding's section with its chunk label for the synthesis pass.

    The synthesis call inspects ``section`` to know which chunk surfaced an
    issue; without this prefix the synthesis prompt cannot tell intra-chunk
    findings apart from cross-discipline ones it might re-emit. The prefix
    is human-readable because it also shows up in the report.
    """
    label = _chunk_label(chunk_id)
    if not label:
        return finding
    section = finding.section or ""
    if label.lower() in section.lower():
        return finding
    finding.section = f"[{label}] {section}".strip().rstrip(":")
    return finding


def _synthesize_chunk_findings(
    chunk_results: list[tuple[str, ReviewResult]],
    *,
    fallback_model: str,
) -> tuple[list[Finding], str, str]:
    """Combine chunk-level findings into a single ReviewResult payload.

    Returns ``(findings, summary, status)``. We deliberately keep the
    synthesis local — running another remote call would defeat the cost
    motivation behind chunking when most projects fit cleanly in <=2
    chunks. If a true cross-discipline coordination pass is needed in
    future revisions, this function is the place to add it.
    """
    findings: list[Finding] = []
    summaries: list[str] = []
    chunks_completed = 0
    chunks_failed = 0
    chunks_skipped = 0

    for chunk_id, result in chunk_results:
        label = _chunk_label(chunk_id)
        if result.cross_check_status == "completed":
            chunks_completed += 1
            for f in result.findings:
                findings.append(_label_finding_with_chunk(f, chunk_id))
            if result.thinking:
                summaries.append(f"--- {label} ---\n{result.thinking.strip()}")
        elif result.cross_check_status == "skipped":
            chunks_skipped += 1
            summaries.append(f"--- {label} ---\nSkipped: {result.thinking or 'no reason given'}")
        else:
            chunks_failed += 1
            summaries.append(
                f"--- {label} ---\nFailed: {result.error or 'unknown error'}"
            )

    if chunks_completed == 0 and (chunks_failed or chunks_skipped):
        # Every chunk failed/skipped — bubble up a failed status so the GUI
        # surfaces the issue instead of silently showing zero findings.
        status = "failed" if chunks_failed else "skipped"
    else:
        status = "completed"

    summary_header = (
        f"Chunked cross-check ({chunks_completed} completed, "
        f"{chunks_failed} failed, {chunks_skipped} skipped). "
        "Per-chunk summaries follow.\n"
    )
    summary_text = summary_header + "\n\n".join(summaries) if summaries else summary_header
    return findings, summary_text, status


def run_chunked_cross_check(
    specs: list[ExtractedSpec],
    existing_findings: list[Finding],
    *,
    project_context: str = "",
    max_retries: int = 3,
    verbose: bool = False,
    stream_callback: StreamCallback | None = None,
    cycle: CodeCycle = DEFAULT_CYCLE,
    model: str = CROSS_CHECK_MODEL_DEFAULT,
    log: LogFn = _noop_log,
) -> ReviewResult:
    """Run cross-check, chunking by CSI division when the input is too large.

    Plan section 12.3: large projects historically returned a ``skipped``
    status because the combined input exceeded ``CROSS_CHECK_RECOMMENDED_MAX``.
    This wrapper falls back to per-chunk cross-checks (Division 21 / 22 /
    23 / Controls + Commissioning / Project-wide) and merges the chunk-level
    findings into a single :class:`ReviewResult` with the chunk label
    preserved in each finding's ``section``. When the input fits, it
    delegates to the original :func:`run_cross_check` so behavior is
    unchanged for small projects.
    """
    if len(specs) < 2:
        return run_cross_check(
            specs, existing_findings,
            project_context=project_context, max_retries=max_retries,
            verbose=verbose, stream_callback=stream_callback, cycle=cycle, model=model,
        )

    system_prompt = _cross_system_prompt(cycle)
    full_input = _build_cross_check_input(specs, existing_findings)
    full_user = _get_cross_check_user_message(full_input, len(specs), project_context=project_context)
    total_tokens = count_tokens(system_prompt) + count_tokens(full_user)
    if total_tokens <= CROSS_CHECK_RECOMMENDED_MAX:
        return run_cross_check(
            specs, existing_findings,
            project_context=project_context, max_retries=max_retries,
            verbose=verbose, stream_callback=stream_callback, cycle=cycle, model=model,
        )

    chunks = _group_specs_by_chunk(specs)
    if len(chunks) <= 1 or all(len(group) < 2 for _, group in chunks):
        # Cannot meaningfully chunk — surface the original skip so the GUI
        # can warn the user. Better than silently truncating.
        log(
            f"Cross-check input ({total_tokens:,} tokens) exceeds "
            f"{CROSS_CHECK_RECOMMENDED_MAX:,} and cannot be chunked by CSI "
            "division. Skipping cross-check.",
            level="warning",
        )
        return ReviewResult(
            findings=[],
            thinking=(
                f"Combined input ({total_tokens:,}) exceeds cross-check limit "
                f"({CROSS_CHECK_RECOMMENDED_MAX:,}) and chunking by CSI division "
                "did not produce more than one viable chunk."
            ),
            model=model,
            cross_check_status="skipped",
        )

    log(
        f"Cross-check input ({total_tokens:,} tokens) exceeds "
        f"{CROSS_CHECK_RECOMMENDED_MAX:,}. Chunking into "
        f"{len(chunks)} CSI division group(s).",
        level="info",
    )

    chunk_results: list[tuple[str, ReviewResult]] = []
    aggregate_in = aggregate_out = 0
    started = time.time()
    for chunk_id, chunk_specs in chunks:
        label = _chunk_label(chunk_id)
        chunk_filenames = {s.filename for s in chunk_specs}
        scoped_findings = _filter_findings_for_chunk(existing_findings, chunk_filenames)
        log(
            f"Cross-check chunk: {label} ({len(chunk_specs)} spec(s)).",
            level="step",
        )
        chunk_result = run_cross_check(
            chunk_specs,
            scoped_findings,
            project_context=project_context,
            max_retries=max_retries,
            verbose=verbose,
            stream_callback=stream_callback,
            cycle=cycle,
            model=model,
        )
        chunk_results.append((chunk_id, chunk_result))
        aggregate_in += chunk_result.input_tokens
        aggregate_out += chunk_result.output_tokens

    findings, summary_text, status = _synthesize_chunk_findings(
        chunk_results, fallback_model=model
    )
    combined = ReviewResult(
        findings=findings,
        thinking=summary_text,
        model=model,
        input_tokens=aggregate_in,
        output_tokens=aggregate_out,
        elapsed_seconds=time.time() - started,
        cross_check_status=status,
    )
    return combined
