"""Web search verification for Spec Critic findings."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable

from anthropic import APIError, APIConnectionError, RateLimitError

from .batch import submit_verification_batch, poll_batch, retrieve_verification_results, cancel_batch
from .reviewer import Finding, MODEL_OPUS_46, _get_client
from .code_cycles import CodeCycle, DEFAULT_CYCLE

VerifyProgressFn = Callable[[int, int, str], None]


def _noop_verify_progress(_: int, __: int, ___: str) -> None:
    return


@dataclass
class VerificationResult:
    verdict: str
    explanation: str = ""
    sources: list[str] = field(default_factory=list)
    correction: str | None = None


def _build_verification_prompt(finding: Finding, *, cycle: CodeCycle = DEFAULT_CYCLE) -> str:
    return "\n".join([
        "You are a construction specification verification assistant for California projects.",
        "Run a secondary AI verification pass using web search.",
        "Prefer authoritative sources (official code bodies, ICC, NFPA, ASHRAE, DSA). Avoid forums/blogs/AI-generated pages.",
        "If evidence is weak or ambiguous, return UNVERIFIED.",
        "Leave sources as [] if you cannot provide reliable URLs. Do not invent URLs.",
        "Keep explanation to 1-2 sentences.",
        "",
        f"File: {finding.fileName}",
        f"Section: {finding.section}",
        f"Severity: {finding.severity}",
        f"Action: {finding.actionType}",
        f"Issue: {finding.issue}",
        f"Code reference: {finding.codeReference or 'none'}",
        f"Existing text: {finding.existingText or 'none'}",
        f"Suggested replacement: {finding.replacementText or 'none'}",
        "",
        f"Current cycle: CBC {cycle.cbc}, CMC {cycle.cmc}, CPC {cycle.cpc}, CEC {cycle.energy_code}, CALGreen {cycle.calgreen}",
        f"Current seismic standard: ASCE {cycle.asce7}",
        "",
        'Respond with ONLY JSON:',
        '{',
        '  "verdict": "CONFIRMED" | "CORRECTED" | "UNVERIFIED" | "DISPUTED",',
        '  "explanation": "1-2 sentences",',
        '  "sources": ["url1", "url2"],',
        '  "correction": "corrected replacement text or null"',
        '}',
    ])


def _parse_verification_response(response_text: str) -> VerificationResult:
    text = response_text.strip()
    if text.startswith("```"):
        lines = [l for l in text.split("\n") if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return VerificationResult(verdict="UNVERIFIED", explanation=text[:500] or "Verification response did not contain structured JSON.")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return VerificationResult(verdict="UNVERIFIED", explanation="Verification response was not valid JSON.")

    verdict = str(data.get("verdict", "UNVERIFIED")).upper().strip()
    if verdict not in ("CONFIRMED", "CORRECTED", "UNVERIFIED", "DISPUTED"):
        verdict = "UNVERIFIED"
    return VerificationResult(
        verdict=verdict,
        explanation=str(data.get("explanation") or "")[:500],
        sources=[str(s) for s in data.get("sources", []) if s],
        correction=(str(data.get("correction"))[:500] if data.get("correction") is not None else None),
    )


def _cancel_batch_safely(batch_id: str, log: Callable[[str], None]) -> None:
    try:
        cancel_batch(batch_id)
        log(f"Cancellation requested for verification batch {batch_id}.")
    except Exception as e:
        log(f"Could not cancel verification batch {batch_id}: {e}")


def verify_finding(finding: Finding, *, max_retries: int = 2, cycle: CodeCycle = DEFAULT_CYCLE) -> VerificationResult:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return VerificationResult(verdict="UNVERIFIED", explanation="No API key available for verification.")

    client = _get_client()
    prompt = _build_verification_prompt(finding, cycle=cycle)

    for attempt in range(max_retries + 1):
        try:
            response = client.messages.create(model=MODEL_OPUS_46, max_tokens=32_000, tools=[{"type": "web_search_20250305", "name": "web_search"}], messages=[{"role": "user", "content": prompt}])
            stop_reason = getattr(response, "stop_reason", None)
            if stop_reason != "end_turn":
                return VerificationResult(verdict="UNVERIFIED", explanation=f"Verification response incomplete (stop_reason: {stop_reason}).")

            response_text = ""
            search_urls: list[str] = []
            for block in response.content:
                if hasattr(block, "text"):
                    response_text += block.text
                if getattr(block, "type", None) == "web_search_tool_result":
                    for r in (getattr(block, "results", []) or []):
                        url = getattr(r, "url", None)
                        if url:
                            search_urls.append(url)

            if not response_text.strip():
                return VerificationResult(verdict="UNVERIFIED", explanation="Verification produced no text response.")

            parsed = _parse_verification_response(response_text)
            if search_urls:
                existing = set(parsed.sources)
                for url in search_urls:
                    if url not in existing:
                        parsed.sources.append(url)
            return parsed
        except RateLimitError:
            if attempt < max_retries:
                time.sleep(10 * (attempt + 1))
                continue
            return VerificationResult(verdict="UNVERIFIED", explanation="Rate limited during verification.")
        except (APIConnectionError, APIError) as e:
            if attempt < max_retries:
                time.sleep(5 * (attempt + 1))
                continue
            return VerificationResult(verdict="UNVERIFIED", explanation=f"API error during verification: {e}")
        except Exception as e:
            return VerificationResult(verdict="UNVERIFIED", explanation=f"Unexpected error during verification: {e}")


def verify_findings(findings: list[Finding], *, progress: VerifyProgressFn = _noop_verify_progress, cycle: CodeCycle = DEFAULT_CYCLE) -> list[Finding]:
    verifiable = list(findings)
    verifiable.sort(key=lambda f: f.confidence)
    total = len(verifiable)
    for i, f in enumerate(verifiable):
        progress(i + 1, total, f.fileName or "Unknown")
        try:
            f.verification = verify_finding(f, cycle=cycle)
        except Exception as e:
            f.verification = VerificationResult(verdict="UNVERIFIED", explanation=f"Verification crashed: {e}")
    return findings


def verify_findings_batch(findings: list[Finding], *, log: Callable[[str], None] = lambda _: None, progress: Callable[[float, str], None] = lambda _p, _m: None, poll_interval: int = 15, cycle: CodeCycle = DEFAULT_CYCLE) -> list[Finding]:
    if not findings:
        log("No findings eligible for batch verification.")
        return findings

    progress(0.0, f"Submitting {len(findings)} verification requests...")
    job = submit_verification_batch(findings, build_prompt_fn=lambda finding: _build_verification_prompt(finding, cycle=cycle))
    log(f"Verification batch submitted: {job.batch_id}")

    consecutive_errors = 0
    while True:
        try:
            status = poll_batch(job.batch_id)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors >= 5:
                _cancel_batch_safely(job.batch_id, log)
                raise RuntimeError(f"Batch verification failed after 5 poll errors: {e}") from e
            time.sleep(poll_interval * 2)
            continue

        progress(5.0 + (status.progress_pct / 100.0) * 85.0, f"Verification: {status.succeeded}/{status.total} done")
        st = status.status.replace("-", "_")
        if st == "ended":
            break
        if st in ("failed", "expired", "canceled"):
            raise RuntimeError(f"Batch verification terminated with status '{status.status}'.")
        time.sleep(poll_interval)

    retrieve_verification_results(job, findings, parse_response_fn=_parse_verification_response)
    progress(100.0, "Verification complete")
    return findings
