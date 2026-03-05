"""
Cross-spec coordination checker for Spec Critic.

Runs a single API call after per-spec reviews to catch coordination issues
that no single-spec review could find: contradictory values across specs,
missing referenced sections, division-of-work gaps, inconsistent terminology,
and conflicting equipment schedules.

Uses Claude Sonnet 4.6 (not Opus) — coordination checking is a focused
task that doesn't require the full power of Opus, and the input is a
condensed summary (section headers + per-spec findings) rather than full
spec text.

The cross-checker receives:
    - File names and section headers extracted from each spec
    - Per-spec findings from the review pass (so it knows what was already caught)

This keeps token usage low and lets the cross-checker focus exclusively on
inter-spec coordination rather than repeating within-spec analysis.

Design decisions:
    - Sonnet 4.6 for cost/speed (coordination is a focused analytical task)
    - Input is condensed (headers + findings, not full spec text)
    - Only runs when 2+ specs are loaded (single-spec has nothing to coordinate)
    - Optional — controlled by a GUI checkbox
    - Findings use the same Finding dataclass for seamless report integration
    - Rendered in a separate "CROSS-SPEC COORDINATION" section in the report
    - Coordination findings go through deduplication and verification like
      any other findings
    - Graceful skip if combined input exceeds token limit

v1.6.0 — Initial implementation.

Usage:
    from cross_checker import run_cross_check, extract_section_headers

    headers = extract_section_headers(specs)
    result = run_cross_check(headers, existing_findings, project_context="...")
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from anthropic import Anthropic, APIError, APIConnectionError, RateLimitError

from .extractor import ExtractedSpec
from .reviewer import Finding, ReviewResult, _extract_json_array, _parse_findings, _get_api_key, MODEL_SONNET_46
from .tokenizer import RECOMMENDED_MAX, count_tokens

StreamCallback = Callable[[str], None]


# ---------------------------------------------------------------------------
# Section header extraction
# ---------------------------------------------------------------------------

# Common CSI section header patterns in spec documents
_HEADER_PATTERNS = [
    # "PART 1 - GENERAL", "PART 2 - PRODUCTS", "PART 3 - EXECUTION"
    re.compile(r"^PART\s+\d+\s*[-–—]\s*.+", re.IGNORECASE),
    # "1.1 SUMMARY", "2.3 PIPE HANGERS AND SUPPORTS"
    re.compile(r"^\d+\.\d+(?:\.\d+)?\s+[A-Z].*"),
    # "A. Section Includes:" — lettered subsection titles (exclude body text
    # containing verbs like "shall", "must", "provide", "is required")
    re.compile(r"^[A-Z]\.\s+(?!.*\b(?:shall|must|provide|is required)\b).{10,}"),
    # All-caps lines that look like section titles (require ≥2 words)
    re.compile(r"^[A-Z]{2,}(?:\s+[A-Z][A-Z\s,/&()-]*){1,}$"),
]

# Pattern for extracting lines with numeric values relevant to MEP coordination
_VALUE_PATTERN = re.compile(
    r'\d+(?:\.\d+)?\s*(?:°F|°C|psi|psig|GPM|gpm|CFM|cfm|inches|inch|feet|ft|in\.'
    r'|lbs|tons|HP|hp|kW|kw|BTU|Btu|MBH|gallons|gallon|GPH|FPM|fpm)',
    re.IGNORECASE,
)

# Pattern for cross-references to other spec sections
_XREF_PATTERN = re.compile(
    r'(?:refer\s+to|see\s+|per\s+)?(?:Section|Division)\s+\d{2}\s*\d{2}\s*\d{2}',
    re.IGNORECASE,
)


def _is_section_header(line: str) -> bool:
    """Check if a line looks like a CSI specification section header."""
    stripped = line.strip()
    if not stripped or len(stripped) < 4:
        return False
    return any(p.match(stripped) for p in _HEADER_PATTERNS)


def extract_section_headers(spec: ExtractedSpec, max_headers: int = 150) -> list[str]:
    """Extract section headers and structural lines from a spec.

    Returns lines that appear to be CSI section headers (PART lines,
    numbered articles, lettered subsections, all-caps titles). This
    gives the cross-checker enough structural context to identify
    coordination issues without needing full spec text.

    Args:
        spec: Extracted specification content
        max_headers: Maximum number of headers to extract per spec

    Returns:
        List of header strings in document order
    """
    headers: list[str] = []
    for line in spec.content.split("\n"):
        stripped = line.strip()
        if _is_section_header(stripped):
            headers.append(stripped)
            if len(headers) >= max_headers:
                break
    return headers


def _build_spec_summary(
    specs: list[ExtractedSpec],
    existing_findings: list[Finding],
) -> str:
    """Build the condensed input for the cross-checker.

    Combines section headers from each spec and a summary of per-spec
    findings so the cross-checker knows what's already been flagged.

    Returns:
        Formatted string with per-spec headers and existing findings summary
    """
    parts: list[str] = []

    # Section 1: Per-spec structural summaries
    parts.append("=" * 60)
    parts.append("SPECIFICATION STRUCTURE (section headers per file)")
    parts.append("=" * 60)

    for spec in specs:
        headers = extract_section_headers(spec)
        parts.append(f"\n===== FILE: {spec.filename} =====")
        if headers:
            for h in headers:
                parts.append(f"  {h}")
        else:
            parts.append("  (no section headers detected)")

        # Extract opening ~200 words of each Part (scope statements)
        part_pattern = re.compile(r"^(PART\s+\d+\s*[-–—]\s*.+)", re.IGNORECASE | re.MULTILINE)
        part_matches = list(part_pattern.finditer(spec.content))
        if part_matches:
            parts.append("  --- SCOPE EXCERPTS ---")
            for idx, pm in enumerate(part_matches):
                part_title = pm.group(1).strip()
                # Limit excerpt to text before the next PART header
                if idx + 1 < len(part_matches):
                    excerpt_end = part_matches[idx + 1].start()
                else:
                    excerpt_end = len(spec.content)
                after_part = spec.content[pm.end():excerpt_end].strip()
                words = after_part.split()[:200]
                excerpt = " ".join(words)
                if excerpt:
                    parts.append(f"  [{part_title}]")
                    parts.append(f"  {excerpt}")

        # Extract lines with numeric values (temperatures, pressures, etc.)
        value_lines: list[str] = []
        xref_lines: list[str] = []
        for line in spec.content.split("\n"):
            stripped = line.strip()
            if not stripped or len(stripped) > 200:
                continue
            if _VALUE_PATTERN.search(stripped) and len(value_lines) < 30:
                value_lines.append(f"  {stripped}")
            if _XREF_PATTERN.search(stripped) and len(xref_lines) < 15:
                xref_lines.append(f"  {stripped}")

        if value_lines:
            parts.append("  --- KEY VALUES ---")
            parts.extend(value_lines)
        if xref_lines:
            parts.append("  --- CROSS-REFERENCES ---")
            parts.extend(xref_lines)

    # Section 2: Existing findings summary (so cross-checker doesn't repeat them)
    if existing_findings:
        parts.append("")
        parts.append("=" * 60)
        parts.append("ISSUES ALREADY IDENTIFIED (do NOT repeat these)")
        parts.append("=" * 60)

        for f in existing_findings:
            parts.append(
                f"  [{f.severity}] {f.fileName} — {f.section}: "
                f"{f.issue[:120]}{'...' if len(f.issue) > 120 else ''}"
            )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Cross-check system prompt
# ---------------------------------------------------------------------------

_CROSS_CHECK_SYSTEM_PROMPT = """You are a specification coordination reviewer for mechanical and plumbing disciplines. Your ONLY job is to identify cross-spec coordination issues — problems that exist BETWEEN specifications, not within a single spec.

<task>
You are given the section-level structure, key numeric values, and cross-references from multiple specification files, along with a list of issues that have already been identified by a per-spec reviewer. Your job is to find coordination problems that a single-spec review would miss. Flag contradictions only when you can cite the specific conflicting values from the excerpts provided.

DO NOT repeat any issues from the "ISSUES ALREADY IDENTIFIED" list. Those are already caught.
DO NOT flag within-spec issues (wrong code years, formatting, etc.). Those are handled elsewhere.
ONLY flag issues that involve TWO OR MORE specifications interacting.
</task>

<what_to_look_for>
1. CROSS-REFERENCES TO MISSING SPECS — Spec A says "refer to Section 23 64 00" but no 23 64 00 spec is in the set.
2. CONTRADICTORY VALUES — Spec A specifies 42°F CHW supply temp, Spec B specifies 44°F for the same system.
3. DIVISION OF WORK GAPS — Neither the mechanical nor plumbing spec covers a particular scope item (e.g., condensate drain piping, glycol fill systems, expansion tanks).
4. DIVISION OF WORK OVERLAPS — Both specs claim responsibility for the same scope item, creating conflict.
5. INCONSISTENT TERMINOLOGY — Spec A calls it "chilled water" while Spec B calls it "CHW" with different parameters, or Spec A says "air handling unit" while Spec B says "fan coil unit" for what appears to be the same equipment.
6. EQUIPMENT SCHEDULE CONFLICTS — Equipment referenced in one spec doesn't match what's specified in another.
7. MISSING COORDINATION SECTIONS — Specs that should reference each other but don't (e.g., HVAC piping spec should reference the testing spec).
</what_to_look_for>

<what_NOT_to_flag>
- Any issue already in the "ISSUES ALREADY IDENTIFIED" list
- Within-spec issues (code years, formatting, internal contradictions)
- Missing specs that are clearly outside the MEP scope provided
- Minor terminology differences that are clearly just abbreviations (CHW vs chilled water is fine if values match)
- Issues you are not reasonably confident about (below 0.50 confidence)
- Do not infer contradictions from the absence of information — only flag issues where you see explicit conflicting data in the excerpts provided
</what_NOT_to_flag>

<severity_guidance>
Coordination issues are typically HIGH or CRITICAL:
- CRITICAL: Contradictions that could cause construction conflicts, safety issues, or DSA rejection (e.g., conflicting fire ratings, contradictory seismic requirements across specs)
- HIGH: Coordination gaps that will cause RFIs, change orders, or confusion during construction (e.g., missing referenced specs, division of work gaps, contradictory equipment parameters)
- MEDIUM: Inconsistencies that should be cleaned up but won't block construction (e.g., terminology differences, minor parameter mismatches)
- Do NOT use GRIPES for coordination issues — if it's worth flagging cross-spec, it's at least MEDIUM.
</severity_guidance>

<confidence_guidance>
Include a confidence score (0.0-1.0) for each finding:
- 0.85-1.0: You can clearly see the contradiction or gap across specific specs
- 0.60-0.84: You suspect a coordination issue but can't fully confirm from headers alone
- 0.50-0.59: Possible issue, flag with clear caveats
- Below 0.50: Do NOT flag. Mention in narrative only if important.
</confidence_guidance>

<output_format>
First, provide a brief COORDINATION SUMMARY (1-2 paragraphs). Focus on the big picture: how well do these specs coordinate? Are there major gaps or conflicts?

Then output findings as a JSON array wrapped in <FINDINGS_JSON></FINDINGS_JSON> tags (no code fences). Each finding:
- severity: "CRITICAL" | "HIGH" | "MEDIUM"
- fileName: The primary file where the issue manifests (use the filename from the FILE headers)
- section: Best guess at section location, or "Cross-spec coordination" if not specific
- issue: Clear description of the coordination problem, referencing BOTH specs involved
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
    spec_summary: str,
    file_count: int,
    project_context: str = "",
) -> str:
    """Build the user message for the cross-spec coordination check.

    Args:
        spec_summary: Output of _build_spec_summary()
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

This is a COORDINATION-ONLY review. Focus exclusively on issues that exist BETWEEN specs — contradictions, missing references, division-of-work gaps, and inconsistencies across files.

Do NOT repeat any issues from the "ISSUES ALREADY IDENTIFIED" section.

{context_block}{spec_summary}"""


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

    Sends a condensed summary (section headers + existing findings) to
    Sonnet 4.6 and returns coordination-only findings.

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
            model=MODEL_SONNET_46,
        )

    # Build condensed input
    spec_summary = _build_spec_summary(specs, existing_findings)

    # Check token limit before calling API
    system_tokens = count_tokens(_CROSS_CHECK_SYSTEM_PROMPT)
    user_message = _get_cross_check_user_message(
        spec_summary, len(specs), project_context=project_context,
    )
    user_tokens = count_tokens(user_message)
    total_input_tokens = system_tokens + user_tokens

    if total_input_tokens > RECOMMENDED_MAX:
        return ReviewResult(
            findings=[],
            thinking=(
                f"Cross-spec coordination skipped: combined input "
                f"({total_input_tokens:,} tokens) exceeds limit "
                f"({RECOMMENDED_MAX:,} tokens)."
            ),
            model=MODEL_SONNET_46,
        )

    # Make the API call
    client = Anthropic(api_key=_get_api_key())
    start_time = time.time()
    result = ReviewResult(model=MODEL_SONNET_46)

    for attempt in range(max_retries):
        try:
            if verbose:
                print(f"Cross-check call (attempt {attempt + 1}/{max_retries})...")

            with client.messages.stream(
                model=MODEL_SONNET_46,
                max_tokens=16384,
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