"""
Cross-spec coordination checker for Spec Critic.

Runs a single API call after per-spec reviews to catch coordination issues
that no single-spec review could find: contradictory values across specs,
missing referenced sections, division-of-work gaps, inconsistent terminology,
and conflicting equipment schedules.

v2.2.0 — Opus 4.6 + full spec content + 1M context window + adaptive thinking.
    Upgraded from Sonnet 4.6 with condensed headers to Opus 4.6 with
    the full text of every specification. With the 1M token context
    window (GA March 2026, no beta header required, standard pricing),
    the cross-checker can now analyze complete spec content rather than
    relying on section headers and excerpts. Adaptive thinking
    (thinking: {"type": "adaptive"}) is enabled so the model can reason
    deeply about cross-spec coordination before producing findings.
    This enables detection of contradictions in body text, subtle scope
    overlaps, and coordination issues that were invisible with
    headers-only analysis.

v1.6.0 — Initial implementation (Sonnet 4.6 + condensed headers).

The cross-checker receives:
    - Full text content of each specification (with file delimiters)
    - Per-spec findings from the review pass (so it knows what was already caught)

Design decisions:
    - Opus 4.6 for maximum analytical depth on cross-spec coordination
    - Full spec content instead of headers-only (1M context makes this feasible)
    - Only runs when 2+ specs are loaded (single-spec has nothing to coordinate)
    - Optional — controlled by a GUI checkbox
    - Findings use the same Finding dataclass for seamless report integration
    - Rendered in a separate "CROSS-SPEC COORDINATION" section in the report
    - Coordination findings go through deduplication and verification like
      any other findings
    - Graceful skip if combined input exceeds the 1M token limit

Usage:
    from cross_checker import run_cross_check

    result = run_cross_check(specs, existing_findings, project_context="...")
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from anthropic import Anthropic, APIError, APIConnectionError, RateLimitError

from .extractor import ExtractedSpec
from .reviewer import Finding, ReviewResult, _extract_json_array, _parse_findings, _get_api_key, MODEL_OPUS_46
from .tokenizer import CROSS_CHECK_RECOMMENDED_MAX, count_tokens

StreamCallback = Callable[[str], None]


# ---------------------------------------------------------------------------
# Input construction
# ---------------------------------------------------------------------------

def _build_cross_check_input(
    specs: list[ExtractedSpec],
    existing_findings: list[Finding],
) -> str:
    """Build the full-content input for the cross-checker.

    Includes the complete text of each specification (with file delimiters
    matching the per-spec review format) and a summary of existing per-spec
    findings so the cross-checker doesn't repeat them.

    Args:
        specs: List of ExtractedSpec objects with full content
        existing_findings: Per-spec findings already identified

    Returns:
        Formatted string with full spec content and existing findings summary
    """
    parts: list[str] = []

    # Section 1: Full spec content with file delimiters
    for spec in specs:
        parts.append(f"\n===== FILE: {spec.filename} =====")
        parts.append(spec.content)

    # Section 2: Existing findings summary (so cross-checker doesn't repeat them)
    if existing_findings:
        parts.append("")
        parts.append("=" * 60)
        parts.append("ISSUES ALREADY IDENTIFIED BY PER-SPEC REVIEW (do NOT repeat these)")
        parts.append("=" * 60)

        for f in existing_findings:
            parts.append(
                f"  [{f.severity}] {f.fileName} — {f.section}: "
                f"{f.issue[:150]}{'...' if len(f.issue) > 150 else ''}"
            )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Cross-check system prompt
# ---------------------------------------------------------------------------

_CROSS_CHECK_SYSTEM_PROMPT = """You are a specification coordination reviewer for mechanical and plumbing disciplines. Your ONLY job is to identify cross-spec coordination issues — problems that exist BETWEEN specifications, not within a single spec.

<task>
You are given the full text of multiple specification files for a California K-12 project under DSA jurisdiction, along with a list of issues that have already been identified by a per-spec reviewer. Your job is to find coordination problems that a single-spec review would miss.

You have the complete specification content — read it thoroughly. Look for contradictions, gaps, and inconsistencies that only become visible when comparing two or more specs side by side.

DO NOT repeat any issues from the "ISSUES ALREADY IDENTIFIED" list. Those are already caught.
DO NOT flag within-spec issues (wrong code years, formatting, etc.). Those are handled elsewhere.
ONLY flag issues that involve TWO OR MORE specifications interacting.
</task>

<what_to_look_for>
1. CONTRADICTORY VALUES — Spec A specifies 42°F CHW supply temp, Spec B specifies 44°F for the same system. Spec A says 125 psi working pressure, Spec B says 150 psi for the same piping system. One spec says Schedule 40, another says Schedule 80 for the same service.

2. CROSS-REFERENCES TO MISSING SPECS — Spec A says "refer to Section 23 64 00" but no 23 64 00 spec is in the set. Spec A references products "specified in Section 23 21 13" but that section doesn't exist in the submitted documents.

3. DIVISION OF WORK GAPS — Neither the mechanical nor plumbing spec covers a particular scope item (e.g., condensate drain piping from HVAC equipment, glycol fill and test systems, expansion tanks, PRV discharge piping, equipment pads/curbs, roof penetration flashing/curbs).

4. DIVISION OF WORK OVERLAPS — Both specs claim responsibility for the same scope item, creating conflict. For example, both HVAC and plumbing specs specify natural gas piping, or both claim condensate piping.

5. INCONSISTENT EQUIPMENT REFERENCES — Equipment tag numbers, capacities, or counts in one spec don't match another. A schedule in one spec lists different equipment than what's specified in another.

6. CONTRADICTORY REQUIREMENTS — Different installation, testing, or quality requirements for the same type of work. For example, Spec A requires hydrostatic testing at 1.5x working pressure while Spec B says 2x for the same piping system.

7. MISSING COORDINATION SECTIONS — Specs that should reference each other but don't. For example, the HVAC piping spec doesn't reference the testing/balancing spec, or the plumbing spec doesn't reference the common work results spec.

8. INCONSISTENT TERMINOLOGY — Specs using different names for the same system or component in ways that could cause confusion during construction (not just abbreviation differences like CHW vs. chilled water, which are fine).

9. SCOPE BOUNDARY CONFLICTS — Unclear or conflicting points of connection between mechanical and plumbing systems (e.g., where does the plumber's responsibility end and the mechanical contractor's begin for a dual-temperature system).
</what_to_look_for>

<what_NOT_to_flag>
- Any issue already in the "ISSUES ALREADY IDENTIFIED" list
- Within-spec issues (code years, formatting, internal contradictions, missing DSA requirements)
- Missing specs that are clearly outside the MEP scope provided
- Minor terminology differences that are clearly just abbreviations (CHW vs chilled water is fine if values match)
- Issues you are not reasonably confident about (below 0.50 confidence)
- Do not infer contradictions from the absence of information — only flag issues where you see explicit conflicting data or a clear scope gap
</what_NOT_to_flag>

<severity_guidance>
Coordination issues are typically HIGH or CRITICAL:
- CRITICAL: Contradictions that could cause construction conflicts, safety issues, or DSA rejection (e.g., conflicting fire ratings, contradictory seismic requirements across specs, equipment sizing mismatches that affect life safety)
- HIGH: Coordination gaps that will cause RFIs, change orders, or confusion during construction (e.g., missing referenced specs, division of work gaps, contradictory equipment parameters, scope boundary conflicts)
- MEDIUM: Inconsistencies that should be cleaned up but won't block construction (e.g., terminology differences, minor parameter mismatches, missing cross-references that are nice-to-have)
- Do NOT use GRIPES for coordination issues — if it's worth flagging cross-spec, it's at least MEDIUM.
</severity_guidance>

<confidence_guidance>
Include a confidence score (0.0-1.0) for each finding:
- 0.85-1.0: You can clearly see the contradiction or gap with specific text from both specs
- 0.60-0.84: You see a likely coordination issue but the conflict isn't 100% explicit
- 0.50-0.59: Possible issue, flag with clear caveats about what you're uncertain about
- Below 0.50: Do NOT flag. Mention in narrative only if important.
</confidence_guidance>

<output_format>
First, provide a COORDINATION SUMMARY (1-3 paragraphs). Assess how well these specs coordinate overall. Are there major structural gaps? Do the specs reference each other properly? Is the division of work clear? Be specific — cite the actual spec filenames and what you observed.

Then output findings as a JSON array wrapped in <FINDINGS_JSON></FINDINGS_JSON> tags (no code fences). Each finding:
- severity: "CRITICAL" | "HIGH" | "MEDIUM"
- fileName: The primary file where the issue manifests (use the filename from the FILE headers)
- section: Best guess at section location (e.g., "Part 2, Article 2.3"), or "Cross-spec coordination" if not specific to a section
- issue: Clear description of the coordination problem, referencing BOTH specs involved by filename
- actionType: "ADD" | "EDIT" | "DELETE"
- existingText: The conflicting text from the primary file (null if ADD)
- replacementText: Suggested fix or what should be added (null if DELETE)
- codeReference: Applicable code or standard if relevant (null otherwise)
- confidence: 0.0-1.0

If no coordination issues are found, return an empty array:

<FINDINGS_JSON>
[]
</FINDINGS_JSON>

"""


def _get_cross_check_user_message(
    spec_input: str,
    file_count: int,
    project_context: str = "",
) -> str:
    """Build the user message for the cross-spec coordination check.

    Args:
        spec_input: Output of _build_cross_check_input() (full content + findings)
        file_count: Number of spec files
        project_context: Optional project description

    Returns:
        Formatted user message string
    """
    context_block = ""
    if project_context.strip():
        context_block = f"""
<project_context>
{project_context.strip()}
</project_context>

"""

    return f"""Review the following {file_count} specification documents for cross-spec coordination issues.

This is a COORDINATION-ONLY review. Focus exclusively on issues that exist BETWEEN specs — contradictions, missing references, division-of-work gaps, and inconsistencies across files. You have the FULL text of each specification.

Do NOT repeat any issues from the "ISSUES ALREADY IDENTIFIED" section.

{context_block}{spec_input}"""


# ---------------------------------------------------------------------------
# Cross-check API call
# ---------------------------------------------------------------------------

def run_cross_check(
    specs: list[ExtractedSpec],
    existing_findings: list[Finding],
    *,
    project_context: str = "",
    max_retries: int = 3,
    verbose: bool = False,
    stream_callback: StreamCallback | None = None,
) -> ReviewResult:
    """Run the cross-spec coordination check.

    Sends full specification content and existing findings to Opus 4.6
    and returns coordination-only findings. Uses the 1M token context
    window for complete cross-spec analysis.

    Args:
        specs: List of ExtractedSpec objects (need 2+ for coordination)
        existing_findings: Per-spec findings already identified
        project_context: Optional project description
        max_retries: Maximum retry attempts for transient API errors
        verbose: If True, print debug info
        stream_callback: Optional callback for streaming chunks

    Returns:
        ReviewResult with coordination findings. If fewer than 2 specs or
        token limit exceeded, returns an empty ReviewResult with an
        explanatory message in .thinking.
    """
    # Guard: need at least 2 specs for cross-spec coordination
    if len(specs) < 2:
        return ReviewResult(
            findings=[],
            thinking="Cross-spec coordination skipped: only 1 spec provided.",
            model=MODEL_OPUS_46,
        )

    # Build full-content input
    spec_input = _build_cross_check_input(specs, existing_findings)

    # Check token limit before calling API
    system_tokens = count_tokens(_CROSS_CHECK_SYSTEM_PROMPT)
    user_message = _get_cross_check_user_message(
        spec_input, len(specs), project_context=project_context,
    )
    user_tokens = count_tokens(user_message)
    total_input_tokens = system_tokens + user_tokens

    if total_input_tokens > CROSS_CHECK_RECOMMENDED_MAX:
        return ReviewResult(
            findings=[],
            thinking=(
                f"Cross-spec coordination skipped: combined input "
                f"({total_input_tokens:,} tokens) exceeds the cross-check limit "
                f"({CROSS_CHECK_RECOMMENDED_MAX:,} tokens). "
                f"Try reviewing fewer specs at once."
            ),
            model=MODEL_OPUS_46,
        )

    if verbose:
        print(
            f"Cross-check input: {total_input_tokens:,} tokens "
            f"({total_input_tokens / CROSS_CHECK_RECOMMENDED_MAX * 100:.1f}% of limit)"
        )

    # Make the API call with adaptive thinking enabled.
    # Thinking tokens + text output share the max_tokens budget.
    # We use 65536 to give the model room for deep reasoning about
    # cross-spec coordination while keeping input capacity high.
    client = Anthropic(api_key=_get_api_key())
    start_time = time.time()
    result = ReviewResult(model=MODEL_OPUS_46)

    for attempt in range(max_retries):
        try:
            if verbose:
                print(f"Cross-check call (attempt {attempt + 1}/{max_retries})...")

            with client.messages.stream(
                model=MODEL_OPUS_46,
                max_tokens=65536,
                thinking={"type": "adaptive"},
                system=_CROSS_CHECK_SYSTEM_PROMPT,
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

        except RateLimitError:
            wait_time = 2 ** attempt * 10
            if verbose:
                print(f"Rate limit. Waiting {wait_time}s...")
            time.sleep(wait_time)

        except APIConnectionError:
            wait_time = 2 ** attempt * 5
            if verbose:
                print(f"Connection error. Waiting {wait_time}s...")
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