"""
Web search self-verification for Spec Critic findings.

Uses Claude Sonnet 4.6 with the web_search_20250305 tool to verify
each finding from the review. The verifier treats the entire finding —
issue, suggested fix, and any code citation — as claims to be checked.

Verification uses Sonnet (not Opus) because:
    - Verification is a focused task (check one finding at a time)
    - Sonnet is significantly cheaper and faster
    - The web search tool does the heavy lifting

v1.9.1 — Bug fixes.
    Added generic Exception handler to verify_finding() so unexpected
    errors produce UNVERIFIED instead of crashing the entire verification
    pass. Added per-finding try/except in verify_findings() so a single
    finding failure doesn't abort verification of remaining findings.

v1.7.0 — Verification batching.
    Added verify_findings_batch() as the batch-mode alternative to
    verify_findings(). Submits all verifiable findings as a single
    Anthropic Message Batch, polls until complete, then collects results.
    Used by pipeline.collect_batch_results() when running in batch mode.

v1.5.0 — Broadened verification scope:
    - All CRITICAL, HIGH, and MEDIUM findings are verified regardless of
      whether they include a code reference. Findings without citations
      are still checked for issue validity and fix correctness.
    - The verification prompt now evaluates the full finding holistically:
      is the issue real, is the suggested fix correct, and is any cited
      code/standard accurate (with skepticism, since citations may be
      hallucinated by the review model).
    - Findings are verified in ascending confidence order — low-confidence
      findings are checked first since they are most likely to be wrong.

Each finding produces a VerificationResult with:
    - verdict: CONFIRMED, CORRECTED, UNVERIFIED, or DISPUTED
    - explanation: Brief rationale for the verdict
    - sources: List of URLs consulted
    - correction: Updated replacement text if the finding was wrong

Design decisions:
    - All other findings are verified — with or without a code reference
    - The verifier treats the review model's code citations with skepticism
      since they may be hallucinated
    - Verification is independent per finding — no cross-finding context
    - The verifier never modifies the original Finding; it populates the
      verification field with a VerificationResult
    - Findings are verified in ascending confidence order — low-confidence
      findings are checked first since they are most likely to be wrong
"""

from __future__ import annotations

import json
import os
import time
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
    """Determine if a finding should be verified via web search.

    All findings are verified
    """
    return True


def _build_verification_prompt(finding: Finding) -> str:
    """Build a holistic verification prompt for a single finding.

    The prompt instructs the verifier to evaluate the ENTIRE finding:
        1. Is the identified issue real and accurate?
        2. Is the suggested fix (replacement text) correct?
        3. If a code/standard is cited, is that citation accurate?
           (Citations may be hallucinated — verify them, don't trust them.)

    This is broader than the previous approach which only confirmed
    code references.
    """
    parts = [
        "You are a construction specification verification assistant for "
        "California projects. Another AI model reviewed a specification and "
        "produced the finding below. Your job is to independently verify "
        "whether this finding is correct by searching the web.",
        "",
        "IMPORTANT: The review model may have hallucinated code references, "
        "section numbers, standard editions, or requirements. Do NOT assume "
        "any citation is accurate. Verify everything independently.",
        "",
        "═══════════════════════════════════════",
        "FINDING TO VERIFY:",
        "═══════════════════════════════════════",
        f"  File: {finding.fileName}",
        f"  Section: {finding.section}",
        f"  Severity: {finding.severity}",
        f"  Action: {finding.actionType}",
        f"  Confidence: {finding.confidence:.0%}",
        f"  Issue: {finding.issue}",
    ]

    if finding.codeReference:
        parts.append(f"  Code Reference (UNVERIFIED — may be hallucinated): {finding.codeReference}")

    if finding.existingText:
        parts.append(f"  Existing Text: {finding.existingText}")
    if finding.replacementText:
        parts.append(f"  Suggested Replacement: {finding.replacementText}")

    parts.extend([
        "",
        "═══════════════════════════════════════",
        "YOUR TASK:",
        "═══════════════════════════════════════",
        "",
        "Evaluate ALL of the following:",
        "",
        "1. IS THE ISSUE REAL?",
        "   Is the problem described in the finding actually a problem?",
        "   Search for the relevant code, standard, or best practice to confirm.",
        "",
        "2. IS THE SUGGESTED FIX CORRECT?",
        "   If the finding suggests replacement text, is that replacement accurate?",
        "   Check specific values (temperatures, pressures, dimensions, section numbers).",
        "",
        "3. IS THE CODE REFERENCE ACCURATE? (if one is cited)",
        "   Does the cited code section, standard, or requirement actually exist?",
        "   Does it say what the finding claims it says?",
        "   Is the edition/year correct for the current California code cycle?",
        "",
        "CONTEXT:",
        "- These are California construction projects under DSA jurisdiction",
        "- Current code cycle: CBC 2025, CMC 2025, CPC 2025, CEC 2025, CALGreen 2025",
        "- Current seismic standard: ASCE 7-22",
        "- Focus on California-specific requirements when CBC, CMC, CPC, or ",
        "  California Energy Code is referenced",
        "",
        "Respond with ONLY a JSON object (no other text) in this exact format:",
        '{',
        '  "verdict": "CONFIRMED" | "CORRECTED" | "UNVERIFIED" | "DISPUTED",',
        '  "explanation": "Brief 1-3 sentence rationale - null if verdict is CONFIRMED,',
        '  "sources": ["url1", "url2"],',
        '  "correction": "Corrected replacement text if CORRECTED, or explanation of what is wrong if DISPUTED, else null"',
        '}',
        "",
        "VERDICT MEANINGS:",
        "- CONFIRMED: The issue is real, the suggested fix is correct, and any "
        "code citation is accurate. Set explanation to null.",
        "- CORRECTED: The issue is real, but the suggested fix or code citation "
        "has an error. Provide the corrected text in the correction field.",
        "- UNVERIFIED: Could not find enough authoritative evidence to confirm "
        "or deny. This is acceptable — not everything is freely available online.",
        "- DISPUTED: The finding appears to be incorrect — the issue is not real, "
        "the cited requirement does not exist, or the finding fundamentally "
        "misinterprets the code/standard.",
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

    Evaluates the entire finding holistically: issue validity, fix
    correctness, and citation accuracy (if any citation is present).

    Args:
        finding: The Finding to verify
        max_retries: Number of retries on transient errors

    Returns:
        VerificationResult with verdict, explanation, sources, and optional correction
    """
    
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
                time.sleep(10 * (attempt + 1))
                continue
            return VerificationResult(
                verdict="UNVERIFIED",
                explanation="Rate limited during verification.",
            )
        except (APIConnectionError, APIError) as e:
            if attempt < max_retries:
                time.sleep(5 * (attempt + 1))
                continue
            return VerificationResult(
                verdict="UNVERIFIED",
                explanation=f"API error during verification: {e}",
            )
        except Exception as e:
            # Catch-all for unexpected errors (TypeError, AttributeError,
            # network-level exceptions, etc.) — return UNVERIFIED instead
            # of crashing the entire verification pass.
            return VerificationResult(
                verdict="UNVERIFIED",
                explanation=f"Unexpected error during verification: {e}",
            )


def verify_findings(
    findings: list[Finding],
    *,
    progress: VerifyProgressFn = _noop_verify_progress,
) -> list[Finding]:
    """Verify all eligible findings and populate their verification field.

    Modifies findings in-place by setting finding.verification to a
    VerificationResult. 

    Findings are verified in ascending confidence order — low-confidence
    findings are checked first since they are most likely to be wrong and
    benefit most from verification.

    v1.9.1: Added per-finding try/except so a single verification failure
    does not abort the remaining findings. Previously, an uncaught exception
    from verify_finding() would propagate up through the loop and crash
    the entire pipeline — all findings verified before the crash were lost
    because PipelineResult was never constructed.

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

    # Verify eligible findings sequentially (lowest confidence first).
    # Each finding is wrapped in its own try/except so a single failure
    # does not abort verification of the remaining findings.
    for i, f in enumerate(verifiable):
        progress(i + 1, total, f.fileName or "Unknown")
        try:
            f.verification = verify_finding(f)
        except Exception as e:
            # This should rarely fire since verify_finding() now has its
            # own catch-all, but belt-and-suspenders ensures we never
            # lose the rest of the findings to a single error.
            f.verification = VerificationResult(
                verdict="UNVERIFIED",
                explanation=f"Verification crashed: {e}",
            )

    return findings


def verify_findings_batch(
    findings: list[Finding],
    *,
    log: Callable[[str], None] = lambda _: None,
    progress: Callable[[float, str], None] = lambda _p, _m: None,
    poll_interval: int = 15,
) -> list[Finding]:
    """Verify findings via the Anthropic Message Batches API (50% cost savings).

    Batch-mode alternative to verify_findings(). Submits all verifiable
    findings as a single batch, polls until complete, then collects results.
    Used by pipeline.collect_batch_results() when running in batch mode.

    The batch verification follows the same logic as sequential verification:
    findings are ordered by ascending confidence
    in the batch submission.

    Args:
        findings: List of Finding objects to verify (modified in-place)
        log: Callback for log messages
        progress: Callback for progress updates (percent, message)
        poll_interval: Seconds between poll requests (default: 15)

    Returns:
        The same list of findings (modified in-place) for convenience
    """
    from .batch import (
        submit_verification_batch,
        poll_batch,
        retrieve_verification_results,
    )

    verifiable_count = sum(1 for f in findings if _should_verify(f))
    if verifiable_count == 0:
        log("No findings eligible for batch verification.")
        return findings

    # Submit verification batch
    log(f"Submitting {verifiable_count} findings for batch verification (50% savings)...")
    progress(0.0, f"Submitting {verifiable_count} verification requests...")

    try:
        job = submit_verification_batch(
            findings,
            build_prompt_fn=_build_verification_prompt,
        )
    except Exception as e:
        log(f"Batch verification submission failed: {e}. Falling back to sequential.")
        # Fall back to sequential verification
        def _seq_progress(current: int, total: int, filename: str):
            pct = (current / total) * 100.0 if total > 0 else 100.0
            progress(pct, f"Verifying finding {current}/{total} ({filename})...")
        return verify_findings(findings, progress=_seq_progress)

    log(f"Verification batch submitted: {job.batch_id}")
    progress(5.0, "Verification batch submitted — polling...")

    # Poll until complete
    while True:
        try:
            status = poll_batch(job.batch_id)
        except Exception as e:
            log(f"Poll error (retrying): {e}")
            time.sleep(poll_interval * 2)
            continue

        batch_pct = 5.0 + (status.progress_pct / 100.0) * 85.0
        progress(batch_pct, f"Verification: {status.succeeded}/{status.total} done")

        log(
            f"  Verify batch: {status.succeeded} done, "
            f"{status.processing} processing, {status.errored} errors "
            f"• {status.progress_pct:.0f}%"
        )

        if status.status == "ended":
            break
        elif status.status in ("canceling",):
            log("Verification batch is being canceled...")
        elif status.status in ("failed", "expired", "canceled"):
            # Terminal failure — stop polling, fall back to sequential
            log(f"Verification batch terminated: {status.status}. Falling back to sequential.")
            def _seq_progress(current: int, total: int, filename: str):
                pct = (current / total) * 100.0 if total > 0 else 100.0
                progress(pct, f"Verifying finding {current}/{total} ({filename})...")
            return verify_findings(findings, progress=_seq_progress)

        time.sleep(poll_interval)

    # Collect results
    progress(92.0, "Collecting verification results...")
    log("Collecting verification batch results...")

    try:
        retrieve_verification_results(
            job,
            findings,
            parse_response_fn=_parse_verification_response,
        )
    except Exception as e:
        log(f"Error collecting verification results: {e}")
        # Set UNVERIFIED for any findings without a result
        for f in findings:
            if f.verification is None:
                f.verification = VerificationResult(
                    verdict="UNVERIFIED",
                    explanation=f"Batch result collection failed: {e}",
                )

    progress(100.0, "Verification complete")
    return findings