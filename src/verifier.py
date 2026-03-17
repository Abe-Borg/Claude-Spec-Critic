"""
Web search self-verification for Spec Critic findings.

Uses Claude Opus 4.6 with the web_search_20250305 tool to verify
each finding from the review. The verifier treats the entire finding —
issue, suggested fix, and any code citation — as claims to be checked.

v2.3.0 — Opus-only verification.
    All verification calls use Claude Opus 4.6.
    The entire Spec Critic pipeline uses a single model.

v2.2.0 — Removed artificial polling timeout.

v2.1.1 — Removed sequential fallbacks from batch verification.

v1.9.1 — Bug fixes (generic Exception handler in verify_finding).

v1.7.0 — Verification batching via Anthropic Message Batches API.

v1.5.0 — Broadened verification scope to all findings.

Each finding produces a VerificationResult with:
    - verdict: CONFIRMED, CORRECTED, UNVERIFIED, or DISPUTED
    - explanation: Brief rationale for the verdict
    - sources: List of URLs consulted
    - correction: Updated replacement text if the finding was wrong
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from anthropic import Anthropic, APIError, APIConnectionError, RateLimitError

from .batch import (
    submit_verification_batch,
    poll_batch,
    retrieve_verification_results,
    cancel_batch,
)
from .reviewer import Finding, MODEL_OPUS_46

VerifyProgressFn = Callable[[int, int, str], None]  # current, total, filename


def _noop_verify_progress(_: int, __: int, ___: str) -> None:
    return


@dataclass
class VerificationResult:
    """Result of web search verification for a single finding."""
    verdict: str                        # CONFIRMED, CORRECTED, UNVERIFIED, DISPUTED
    explanation: str = ""
    sources: list[str] = field(default_factory=list)
    correction: str | None = None


def _build_verification_prompt(finding: Finding) -> str:
    """Build a holistic verification prompt for a single finding."""
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
        "Respond with ONLY a JSON object (no other text, no preamble, no markdown fences):",
        '{',
        '  "verdict": "CONFIRMED" | "CORRECTED" | "UNVERIFIED" | "DISPUTED",',
        '  "explanation": "1-2 sentences MAXIMUM. Null if CONFIRMED. Do NOT repeat the original finding or restate the issue.",',
        '  "sources": ["url1", "url2"],',
        '  "correction": "If CORRECTED: the corrected replacement text ONLY (not an explanation). If DISPUTED: 1 sentence why. Otherwise null."',
        '}',
        "",
        "CRITICAL FORMAT RULES:",
        "- explanation must be 1-2 sentences. Do NOT write paragraphs.",
        "- correction must contain ONLY the corrected text or a single sentence. Do NOT explain the code cycle or repeat context.",
        "- Do NOT restate the original issue in your response.",
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
    """Parse the verification model's JSON response into a VerificationResult."""
    text = response_text.strip()

    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1 or end < start:
        explanation = text.strip()
        if len(explanation) > 500:
            explanation = explanation[:500].rsplit(" ", 1)[0] + "\u2026"
        return VerificationResult(
            verdict="UNVERIFIED",
            explanation=explanation or "Verification response did not contain structured JSON.",
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

    MAX_EXPLANATION_CHARS = 500
    MAX_CORRECTION_CHARS = 500

    raw_explanation = data.get("explanation", "")
    explanation = str(raw_explanation) if raw_explanation is not None else ""
    if len(explanation) > MAX_EXPLANATION_CHARS:
        explanation = explanation[:MAX_EXPLANATION_CHARS].rsplit(" ", 1)[0] + "\u2026"

    raw_correction = data.get("correction")
    correction = str(raw_correction) if raw_correction is not None else None
    if correction and len(correction) > MAX_CORRECTION_CHARS:
        correction = correction[:MAX_CORRECTION_CHARS].rsplit(" ", 1)[0] + "\u2026"

    return VerificationResult(
        verdict=verdict,
        explanation=explanation,
        sources=[str(s) for s in data.get("sources", []) if s],
        correction=correction,
    )


def _cancel_batch_safely(batch_id: str, log: Callable[[str], None]) -> None:
    try:
        cancel_batch(batch_id)
        log(f"Cancellation requested for verification batch {batch_id}.")
    except Exception as e:
        log(f"Could not cancel verification batch {batch_id}: {e}")


def verify_finding(finding: Finding, *, max_retries: int = 2) -> VerificationResult:
    """Verify a single finding using Opus 4.6 with web search."""
    
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
                model=MODEL_OPUS_46,
                max_tokens=1024,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}],
            )

            response_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    response_text += block.text

            if not response_text.strip():
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
            return VerificationResult(
                verdict="UNVERIFIED",
                explanation=f"Unexpected error during verification: {e}",
            )


def verify_findings(
    findings: list[Finding],
    *,
    progress: VerifyProgressFn = _noop_verify_progress,
) -> list[Finding]:
    """Verify all findings sequentially (real-time mode). Modifies findings in-place."""
    verifiable = list(findings)
    verifiable.sort(key=lambda f: f.confidence)

    total = len(verifiable)

    for i, f in enumerate(verifiable):
        progress(i + 1, total, f.fileName or "Unknown")
        try:
            f.verification = verify_finding(f)
        except Exception as e:
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

    IMPORTANT (v2.1.1): This function NEVER falls back to sequential
    verification. If batch verification fails, a RuntimeError is raised.

    No artificial timeout (v2.2.0): Polling runs until terminal status.
    """
    verifiable_count = len(findings)
    if verifiable_count == 0:
        log("No findings eligible for batch verification.")
        return findings

    log(f"Submitting {verifiable_count} findings for batch verification (50% savings)...")
    progress(0.0, f"Submitting {verifiable_count} verification requests...")

    try:
        job = submit_verification_batch(
            findings,
            build_prompt_fn=_build_verification_prompt,
        )
    except Exception as e:
        raise RuntimeError(
            f"Batch verification submission failed: {e}. "
            f"Findings will be returned without verification."
        ) from e

    log(f"Verification batch submitted: {job.batch_id}")
    progress(5.0, "Verification batch submitted — polling...")

    consecutive_errors = 0
    while True:
        try:
            status = poll_batch(job.batch_id)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors >= 5:
                log("5 consecutive poll errors. Canceling verification batch.")
                _cancel_batch_safely(job.batch_id, log)
                raise RuntimeError(
                    f"Batch verification failed after 5 consecutive poll errors. "
                    f"Last error: {e}. Findings will be returned without verification."
                ) from e
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

        normalized_status = status.status.replace("-", "_")

        if normalized_status == "ended":
            break
        elif normalized_status == "canceling":
            log("Verification batch is being canceled...")
        elif normalized_status in ("failed", "expired", "canceled"):
            log(f"Verification batch terminated with status: {status.status}")
            raise RuntimeError(
                f"Batch verification terminated with status '{status.status}'. "
                f"Findings will be returned without verification."
            )

        time.sleep(poll_interval)

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
        for f in findings:
            if f.verification is None:
                f.verification = VerificationResult(
                    verdict="UNVERIFIED",
                    explanation=f"Batch result collection failed: {e}",
                )

    progress(100.0, "Verification complete")
    return findings