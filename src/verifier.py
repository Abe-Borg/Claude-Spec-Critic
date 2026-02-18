"""
Web search self-verification for Spec Critic findings.

Uses Claude Sonnet 4.6 with the web_search_20250305 tool to fact-check
each finding from the review. The verifier asks a focused question about
the finding's claim, searches the web, and returns a structured verdict.

Verification uses Sonnet (not Opus) because:
    - Verification is a simpler task (binary fact-check, not nuanced review)
    - Sonnet is significantly cheaper and faster
    - The web search tool does the heavy lifting

v1.5.0 — Updated to Sonnet 4.6. Verification now processes findings in
    ascending confidence order (lowest confidence first) so the findings
    most likely to be wrong get fact-checked first.

Each finding produces a VerificationResult with:
    - verdict: CONFIRMED, CORRECTED, UNVERIFIED, or DISPUTED
    - explanation: Brief rationale for the verdict
    - sources: List of URLs consulted
    - correction: Updated replacement text if the finding was wrong

Design decisions:
    - Only findings with a codeReference are verified (editorial/formatting
      findings skip verification since there's nothing factual to check)
    - GRIPES severity findings are skipped (not worth the API cost)
    - Verification is independent per finding — no cross-finding context
    - The verifier never modifies the original Finding; it populates the
      verification field with a VerificationResult
    - Findings are verified in ascending confidence order — low-confidence
      findings are checked first since they are most likely to be wrong
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from anthropic import Anthropic, APIError, APIConnectionError, RateLimitError

from .reviewer import Finding

# Sonnet 4.6 for verification (cheaper, faster, sufficient for fact-checking)
MODEL_SONNET = "claude-sonnet-4-6"

VerifyProgressFn = Callable[[int, int, str], None]  # current, total, filename


def _noop_verify_progress(_: int, __: int, ___: str) -> None:
    return


@dataclass
class VerificationResult:
    """Result of web search verification for a single finding.

    Attributes:
        verdict: CONFIRMED (finding is correct), CORRECTED (finding intent
            is right but details need adjustment), UNVERIFIED (couldn't find
            evidence either way), or DISPUTED (finding appears incorrect)
        explanation: Brief rationale for the verdict (1-2 sentences)
        sources: List of URLs that were consulted during verification
        correction: If verdict is CORRECTED or DISPUTED, the updated
            replacement text. None otherwise.
    """
    verdict: str                        # CONFIRMED, CORRECTED, UNVERIFIED, DISPUTED
    explanation: str = ""
    sources: list[str] = field(default_factory=list)
    correction: str | None = None


def _should_verify(finding: Finding) -> bool:
    """Determine if a finding is worth verifying via web search.

    Skip findings that are:
        - GRIPES severity (editorial, not worth API cost)
        - Missing a code reference (nothing factual to check)
    """
    if finding.severity == "GRIPES":
        return False
    if not finding.codeReference:
        return False
    return True


def _build_verification_prompt(finding: Finding) -> str:
    """Build a focused verification prompt for a single finding.

    The prompt instructs the model to:
        1. Search the web for the specific code/standard claim
        2. Verify whether the finding's assertion is correct
        3. Return a structured JSON verdict
    """
    parts = [
        "You are a construction code verification assistant. Your job is to "
        "fact-check a specific finding from a specification review by searching "
        "the web for authoritative sources.",
        "",
        "FINDING TO VERIFY:",
        f"  File: {finding.fileName}",
        f"  Section: {finding.section}",
        f"  Severity: {finding.severity}",
        f"  Issue: {finding.issue}",
        f"  Code Reference: {finding.codeReference}",
        f"  Confidence: {finding.confidence:.2f}",
    ]

    if finding.existingText:
        parts.append(f"  Existing Text: {finding.existingText}")
    if finding.replacementText:
        parts.append(f"  Suggested Replacement: {finding.replacementText}")

    parts.extend([
        "",
        "INSTRUCTIONS:",
        "1. Use the web search tool to find the specific code section, standard, "
        "or requirement referenced in this finding.",
        "2. Verify whether the finding's claim is accurate — does the cited code "
        "or standard actually say what the finding claims?",
        "3. Pay attention to code editions/years — a requirement that exists in "
        "one code cycle may not exist in another.",
        "4. Focus on California-specific codes when CBC, CMC, CPC, or California "
        "Energy Code is referenced.",
        "",
        "Respond with ONLY a JSON object (no other text) in this exact format:",
        '{',
        '  "verdict": "CONFIRMED" | "CORRECTED" | "UNVERIFIED" | "DISPUTED",',
        '  "explanation": "Brief 1-2 sentence rationale",',
        '  "sources": ["url1", "url2"],',
        '  "correction": "Updated replacement text if CORRECTED/DISPUTED, else null"',
        '}',
        "",
        "Verdict meanings:",
        "- CONFIRMED: The finding is factually correct. The code/standard says "
        "what the finding claims.",
        "- CORRECTED: The finding identifies a real issue, but the suggested "
        "replacement text has an error (wrong section number, wrong edition year, "
        "etc.). Provide the corrected text in the correction field.",
        "- UNVERIFIED: Could not find authoritative evidence to confirm or deny "
        "the claim. The source may not be freely available online.",
        "- DISPUTED: The finding appears to be incorrect. The code/standard does "
        "NOT say what the finding claims, or the requirement doesn't exist.",
    ])

    return "\n".join(parts)


def _parse_verification_response(response_text: str) -> VerificationResult:
    """Parse the verification model's JSON response into a VerificationResult.

    Handles cases where the model wraps JSON in markdown code fences or
    includes preamble text before the JSON.
    """
    text = response_text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```) and last line (```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    # Find JSON object
    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1:
        return VerificationResult(
            verdict="UNVERIFIED",
            explanation="Could not parse verification response.",
        )

    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return VerificationResult(
            verdict="UNVERIFIED",
            explanation="Verification response was not valid JSON.",
        )

    verdict = str(data.get("verdict", "UNVERIFIED")).upper().strip()
    if verdict not in ("CONFIRMED", "CORRECTED", "UNVERIFIED", "DISPUTED"):
        verdict = "UNVERIFIED"

    return VerificationResult(
        verdict=verdict,
        explanation=str(data.get("explanation", "")),
        sources=[str(s) for s in data.get("sources", []) if s],
        correction=data.get("correction"),
    )


def verify_finding(finding: Finding, *, max_retries: int = 2) -> VerificationResult:
    """Verify a single finding using web search.

    Args:
        finding: The Finding to fact-check
        max_retries: Number of retries on transient errors

    Returns:
        VerificationResult with verdict, explanation, sources, and optional correction
    """
    if not _should_verify(finding):
        return VerificationResult(
            verdict="UNVERIFIED",
            explanation="Skipped: no code reference to verify.",
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return VerificationResult(
            verdict="UNVERIFIED",
            explanation="No API key available for verification.",
        )

    client = Anthropic(api_key=api_key)
    prompt = _build_verification_prompt(finding)

    for attempt in range(max_retries + 1):
        try:
            response = client.messages.create(
                model=MODEL_SONNET,
                max_tokens=1024,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}],
            )

            # Extract text from response content blocks
            response_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    response_text += block.text

            if not response_text:
                return VerificationResult(
                    verdict="UNVERIFIED",
                    explanation="Verification produced no text response.",
                )

            return _parse_verification_response(response_text)

        except RateLimitError:
            if attempt < max_retries:
                import time
                time.sleep(10 * (attempt + 1))
                continue
            return VerificationResult(
                verdict="UNVERIFIED",
                explanation="Rate limited during verification.",
            )
        except (APIConnectionError, APIError) as e:
            if attempt < max_retries:
                import time
                time.sleep(5 * (attempt + 1))
                continue
            return VerificationResult(
                verdict="UNVERIFIED",
                explanation=f"API error during verification: {e}",
            )


def verify_findings(
    findings: list[Finding],
    *,
    progress: VerifyProgressFn = _noop_verify_progress,
) -> list[Finding]:
    """Verify all eligible findings and populate their verification field.

    Modifies findings in-place by setting finding.verification to a
    VerificationResult. Findings that are skipped (GRIPES, no code ref)
    get an UNVERIFIED result with a skip explanation.

    Findings are verified in ascending confidence order — low-confidence
    findings are checked first since they are most likely to be wrong and
    benefit most from verification.

    Args:
        findings: List of Finding objects to verify
        progress: Callback for progress updates (current, total, filename)

    Returns:
        The same list of findings (modified in-place) for convenience
    """
    verifiable = [f for f in findings if _should_verify(f)]

    # Sort by confidence ascending — verify least confident first
    verifiable.sort(key=lambda f: f.confidence)

    total = len(verifiable)

    # Set skip results for non-verifiable findings
    for f in findings:
        if not _should_verify(f):
            f.verification = VerificationResult(
                verdict="UNVERIFIED",
                explanation=(
                    "Skipped: GRIPES findings are not verified."
                    if f.severity == "GRIPES"
                    else "Skipped: no code reference to verify."
                ),
            )

    # Verify eligible findings sequentially (lowest confidence first)
    for i, f in enumerate(verifiable):
        progress(i + 1, total, f.fileName or "Unknown")
        f.verification = verify_finding(f)

    return findings