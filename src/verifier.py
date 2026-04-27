"""Web search verification for Spec Critic findings."""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from anthropic import APIError, APIConnectionError, APIStatusError, RateLimitError, InternalServerError

from .batch import (
    BatchJob,
    poll_batch,  # Backward-compatibility export for older tests/patching.
    retrieve_verification_results_detailed,
    submit_verification_batch,
    submit_verification_followup_wave,
    _extract_api_error_message,
)
from .batch_runtime import DEFAULT_VERIFICATION_POLL_POLICY, PollPolicy, poll_batch_bounded
from .reviewer import Finding, _get_client
from .code_cycles import CodeCycle, DEFAULT_CYCLE
from .verification_config import CODE_EXECUTION_TOOL, VERIFICATION_MODEL, VERIFICATION_MAX_TOKENS, WEB_SEARCH_TOOL

VerifyProgressFn = Callable[[int, int, str], None]
MAX_VERIFICATION_WAVES = 3
_ERRORED_RETRY_MAX = 75  # Backward-compatibility constant for existing tests/imports.


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
        "Verify the finding below using web search evidence.",
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


def _get_verification_system_prompt(cycle: CodeCycle) -> str:
    return "\n".join([
        "You are a construction specification verification assistant for California K-12 DSA projects.",
        "Your sole job is to verify or dispute a single finding using web search evidence.",
        "",
        "You MUST use web search before rendering a verdict.",
        "Do not speculate; if evidence is weak or ambiguous, return UNVERIFIED.",
        "Do not invent URLs. Leave sources as [] if reliable references are unavailable.",
        "",
        f"Current code cycle: CBC {cycle.cbc}, CMC {cycle.cmc}, CPC {cycle.cpc},",
        f"Energy Code {cycle.energy_code}, CALGreen {cycle.calgreen}, ASCE {cycle.asce7}.",
        "",
        "Prefer authoritative sources in this priority order:",
        "",
        "1. California regulatory authorities:",
        "   dgs.ca.gov, dsa.ca.gov, hcai.ca.gov, bsc.ca.gov, energy.ca.gov,",
        "   osfm.fire.ca.gov, calbo.org",
        "",
        "2. Code publishers with full text:",
        "   up.codes, codes.iccsafe.org, iccsafe.org",
        "",
        "3. Standards organizations:",
        "   nfpa.org, ashrae.org, iapmo.org, smacna.org, aspe.org, astm.org, asce.org",
        "",
        "4. Testing and listing agencies:",
        "   ul.com, fmglobal.com",
        "",
        "5. Major manufacturer technical data:",
        "   greenheck.com, trane.com, carrier.com, watts.com, zurn.com, victaulic.com",
        "",
        "6. Industry associations:",
        "   phccweb.org, mcaa.org, csinet.org, seaoc.org",
        "",
        "7. Healthcare-specific (for HCAI projects):",
        "   fgiguidelines.org, jointcommission.org",
        "",
        "8. Archived or historical standards:",
        "   archive.org",
        "",
        "When these sources don't have what you need, search the broader web.",
        "Any credible primary source is better than returning UNVERIFIED.",
        "Avoid forums, AI-generated content, and secondary summaries when primary sources are available.",
        "",
        "Tool usage guidance:",
        "",
        "- You MUST use web search before rendering a verdict.",
        "- Use code_execution only when you need calculations, data parsing, value comparison, or other computation.",
        "- Do NOT use code_execution as a substitute for source gathering.",
        "- Do NOT use code_execution just to format your JSON response.",
        "- If continuing from a paused turn, finish pending work instead of restarting from scratch.",
    ])


def _collect_search_evidence(message) -> tuple[list[str], int, int]:
    """Return (urls, success_count, error_count) for a single message.

    A search block counts as successful only when it yields at least one
    usable result URL. Error-only result lists count solely as errors so
    they cannot satisfy the external-grounding gate.
    """
    search_urls: list[str] = []
    success_count = 0
    error_count = 0
    for block in getattr(message, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type == "web_search_tool_result":
            block_content = getattr(block, "content", None)
            if block_content is None:
                # Backward-compatible fallback for legacy/mocked objects.
                block_content = getattr(block, "results", None)
            block_added_url = False
            if isinstance(block_content, list):
                for item in block_content:
                    item_type = getattr(item, "type", None)
                    if item_type == "web_search_tool_result_error":
                        error_count += 1
                        continue
                    if item_type not in (None, "web_search_result"):
                        continue
                    url = getattr(item, "url", None)
                    if url:
                        search_urls.append(url)
                        block_added_url = True
            elif getattr(block_content, "type", None) == "web_search_tool_result_error":
                # Anthropic SDK models this as a union:
                # WebSearchToolResultBlock.content can be a WebSearchToolResultError object.
                error_count += 1
            if block_added_url:
                success_count += 1
        elif block_type == "web_search_tool_result_error":
            # Backward-compatible fallback in case SDK/server emits top-level error blocks.
            error_count += 1
    return search_urls, success_count, error_count


def _web_search_count(message) -> int:
    usage = getattr(message, "usage", None)
    server_tool_use = getattr(usage, "server_tool_use", None) if usage else None
    return int(getattr(server_tool_use, "web_search_requests", 0) or 0)


def _search_gate_failure(message) -> str | None:
    _, success_count, error_count = _collect_search_evidence(message)
    web_search_count = _web_search_count(message)
    if success_count > 0:
        return None
    if web_search_count > 0 and error_count > 0:
        return f"Web search attempted but all {error_count} search requests failed."
    if web_search_count > 0:
        return f"Web search attempted ({web_search_count} requests) but no usable results were returned."
    return "Verification did not perform web search. Verdict requires external grounding."


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


@dataclass
class VerificationItemOutcome:
    finding_idx: int
    original_custom_id: str
    classification: str
    parsed_verification: VerificationResult | None = None
    assistant_content_blocks: list | None = None
    unverified_reason: str | None = None


def verify_finding(finding: Finding, *, max_retries: int = 2, cycle: CodeCycle = DEFAULT_CYCLE) -> VerificationResult:
    """Verify a single finding using Claude with web search.

    Uses the streaming API because the web_search_20250305 server tool
    requires streaming — non-streaming messages.create() will fail with
    a "streaming is required" error when server-side tools are active.

    Adaptive thinking is enabled so the model can reason through complex
    code-reference chains before rendering a verdict.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return VerificationResult(verdict="UNVERIFIED", explanation="No API key available for verification.")

    client = _get_client()
    prompt = _build_verification_prompt(finding, cycle=cycle)
    system_prompt = _get_verification_system_prompt(cycle)

    for attempt in range(max_retries + 1):
        try:
            all_responses = []
            messages = [{"role": "user", "content": prompt}]
            max_continuations = 10
            for _ in range(max_continuations + 1):
                # --- Streaming API required for web search server tool ---
                with client.messages.stream(
                    model=VERIFICATION_MODEL,
                    max_tokens=VERIFICATION_MAX_TOKENS,
                    thinking={"type": "adaptive"},
                    system=system_prompt,
                    tools=[WEB_SEARCH_TOOL],
                    messages=messages,
                ) as stream:
                    response = stream.get_final_message()
                all_responses.append(response)
                stop_reason = getattr(response, "stop_reason", None)
                if stop_reason == "end_turn":
                    break
                if stop_reason == "pause_turn":
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": [{"type": "text", "text": "continue"}]})
                    continue
                return VerificationResult(verdict="UNVERIFIED", explanation=f"Verification response incomplete (stop_reason: {stop_reason}).")
            if not all_responses or getattr(all_responses[-1], "stop_reason", None) != "end_turn":
                return VerificationResult(verdict="UNVERIFIED", explanation="Verification did not complete after maximum continuation attempts.")

            response_text = ""
            all_search_urls: list[str] = []
            any_search_success = False
            total_search_errors = 0
            total_web_search_requests = 0
            for resp in all_responses:
                for block in getattr(resp, "content", []) or []:

                    if hasattr(block, "text") and block.text is not None:
                        response_text += block.text

                search_urls, successes, errors = _collect_search_evidence(resp)
                all_search_urls.extend(search_urls)
                if successes > 0:
                    any_search_success = True
                total_search_errors += errors
                total_web_search_requests += _web_search_count(resp)
            if not any_search_success:
                if total_search_errors > 0:
                    return VerificationResult(
                        verdict="UNVERIFIED",
                        explanation=f"Web search attempted but all {total_search_errors} search requests failed.",
                    )
                if total_web_search_requests > 0:
                    return VerificationResult(
                        verdict="UNVERIFIED",
                        explanation=f"Web search attempted ({total_web_search_requests} requests) but no usable results were returned.",
                    )
                return VerificationResult(
                    verdict="UNVERIFIED",
                    explanation="Verification did not perform web search. Verdict requires external grounding.",
                )

            if not response_text.strip():
                return VerificationResult(verdict="UNVERIFIED", explanation="Verification produced no text response.")

            parsed = _parse_verification_response(response_text)
            if all_search_urls:
                existing = set(parsed.sources)
                for url in all_search_urls:
                    if url not in existing:
                        parsed.sources.append(url)
            return parsed
        except RateLimitError:
            if attempt < max_retries:
                time.sleep(10 * (attempt + 1))
                continue
            return VerificationResult(verdict="UNVERIFIED", explanation="Rate limited during verification.")
        except InternalServerError as e:
            if attempt < max_retries:
                time.sleep(15 * (attempt + 1))
                continue
            return VerificationResult(verdict="UNVERIFIED", explanation=f"Server overloaded during verification: {e}")
        except APIStatusError as e:
            if getattr(e, "status_code", None) == 529 or e.__class__.__name__ == "OverloadedError":
                if attempt < max_retries:
                    time.sleep(15 * (attempt + 1))
                    continue
                return VerificationResult(verdict="UNVERIFIED", explanation=f"Server overloaded during verification: {e}")
            if attempt < max_retries:
                time.sleep(5 * (attempt + 1))
                continue
            return VerificationResult(verdict="UNVERIFIED", explanation=f"API error during verification: {e}")
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
    if total == 0:
        return findings
    max_workers = min(5, total)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(verify_finding, f, cycle=cycle): f for f in verifiable}
        completed = 0
        for future in as_completed(futures):
            f = futures[future]
            completed += 1
            try:
                f.verification = future.result()
            except Exception as e:
                f.verification = VerificationResult(verdict="UNVERIFIED", explanation=f"Verification crashed: {e}")
            progress(completed, total, f.fileName or "Unknown")
    return findings


def verify_findings_batch(findings: list[Finding], *, log: Callable[[str], None] = lambda _: None, progress: Callable[[float, str], None] = lambda _p, _m: None, poll_interval: int = 15, cycle: CodeCycle = DEFAULT_CYCLE) -> list[Finding]:
    if not findings:
        log("No findings eligible for batch verification.")
        return findings

    progress(0.0, f"Submitting {len(findings)} verification requests...")
    job = start_verification_batch(findings, cycle=cycle)
    log(f"Verification batch submitted: {job.batch_id}")

    collect_verification_batch_results(job, findings, log=log, progress=progress, poll_interval=poll_interval, cycle=cycle)
    progress(100.0, "Verification complete")
    return findings


def _retry_failed_verifications_realtime(
    findings: list[Finding],
    *,
    cycle: CodeCycle = DEFAULT_CYCLE,
    log: Callable[[str], None] = lambda _: None,
    max_retry_count: int = _ERRORED_RETRY_MAX,
) -> None:
    """No-op retained for import compatibility.

    Previously retried UNVERIFIED findings via real-time streaming API.
    Removed in v2.8.0 as part of batch-only enforcement. Retained because
    external code or tests may import this symbol. Does nothing when called.

    Safe to delete once all downstream imports are confirmed updated.
    """
    pass


def start_verification_batch(findings: list[Finding], *, cycle: CodeCycle = DEFAULT_CYCLE) -> BatchJob:
    return submit_verification_batch(
        findings,
        build_prompt_fn=lambda finding: _build_verification_prompt(finding, cycle=cycle),
        system_prompt_fn=_get_verification_system_prompt,
        cycle=cycle,
    )


def _build_retry_request(prompt: str, *, cycle: CodeCycle) -> dict:
    return {
        "model": VERIFICATION_MODEL,
        "max_tokens": VERIFICATION_MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "system": _get_verification_system_prompt(cycle),
        "tools": [WEB_SEARCH_TOOL],
        "messages": [{"role": "user", "content": prompt}],
    }


def _build_continuation_request(prompt: str, assistant_content_blocks: list, *, cycle: CodeCycle) -> dict:
    return {
        "model": VERIFICATION_MODEL,
        "max_tokens": VERIFICATION_MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "system": _get_verification_system_prompt(cycle),
        "tools": [WEB_SEARCH_TOOL],
        "messages": [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": assistant_content_blocks},
            {"role": "user", "content": [{"type": "text", "text": "continue"}]},
        ],
    }


def _extract_message_text(message) -> str:
    return "".join(block.text for block in getattr(message, "content", []) if hasattr(block, "text") and block.text is not None)


def _classify_wave_results(
    *,
    job: BatchJob,
    findings: list[Finding],
    request_contexts: dict[str, dict],
) -> list[VerificationItemOutcome]:
    detailed = retrieve_verification_results_detailed(job)
    outcomes: list[VerificationItemOutcome] = []
    for custom_id, context in request_contexts.items():
        finding_idx = context["finding_idx"]
        result = detailed.get(custom_id)
        if result is None:
            outcomes.append(VerificationItemOutcome(finding_idx=finding_idx, original_custom_id=custom_id, classification="retry", unverified_reason="Missing batch result"))
            continue
        if result.result.type != "succeeded":
            error_detail = _extract_api_error_message(
                getattr(result.result, "error", None)
            )
            unverified_msg = f"Batch request {result.result.type}"
            if error_detail:
                unverified_msg += f": {error_detail}"
            outcomes.append(VerificationItemOutcome(finding_idx=finding_idx, original_custom_id=custom_id, classification="retry", unverified_reason=unverified_msg))
            continue
        message = result.result.message
        stop_reason = getattr(message, "stop_reason", None)
        if stop_reason == "pause_turn":
            outcomes.append(
                VerificationItemOutcome(
                    finding_idx=finding_idx,
                    original_custom_id=custom_id,
                    classification="continue",
                    # SDK Pydantic models serialize correctly when passed back
                    # into batch request messages. Verified by regression test.
                    assistant_content_blocks=list(getattr(message, "content", []) or []),
                    unverified_reason="pause_turn",
                )
            )
            continue
        if stop_reason != "end_turn":
            outcomes.append(VerificationItemOutcome(finding_idx=finding_idx, original_custom_id=custom_id, classification="terminal_unverified", unverified_reason=f"Verification response incomplete (stop_reason: {stop_reason})."))
            continue
        gate_failure = _search_gate_failure(message)
        if gate_failure:
            outcomes.append(VerificationItemOutcome(finding_idx=finding_idx, original_custom_id=custom_id, classification="terminal_unverified", unverified_reason=gate_failure))
            continue
        response_text = _extract_message_text(message)
        if not response_text.strip():
            outcomes.append(VerificationItemOutcome(finding_idx=finding_idx, original_custom_id=custom_id, classification="terminal_unverified", unverified_reason="Verification produced no text response."))
            continue
        parsed = _parse_verification_response(response_text)
        if parsed.verdict == "UNVERIFIED" and "valid JSON" in (parsed.explanation or ""):
            outcomes.append(VerificationItemOutcome(finding_idx=finding_idx, original_custom_id=custom_id, classification="terminal_unverified", unverified_reason=parsed.explanation))
            continue
        search_urls, _, _ = _collect_search_evidence(message)
        if search_urls:
            existing = set(parsed.sources)
            for url in search_urls:
                if url not in existing:
                    parsed.sources.append(url)
        outcomes.append(VerificationItemOutcome(finding_idx=finding_idx, original_custom_id=custom_id, classification="success", parsed_verification=parsed))
    return outcomes


def collect_verification_batch_results(job: BatchJob, findings: list[Finding], *, log: Callable[[str], None] = lambda _: None, progress: Callable[[float, str], None] = lambda _p, _m: None, poll_interval: int = 15, cycle: CodeCycle = DEFAULT_CYCLE, poll_policy: PollPolicy | None = None, max_waves: int = MAX_VERIFICATION_WAVES) -> list[Finding]:
    if not findings:
        return findings
    policy = poll_policy or PollPolicy(
        poll_interval_seconds=poll_interval,
        max_elapsed_seconds=DEFAULT_VERIFICATION_POLL_POLICY.max_elapsed_seconds,
        max_no_progress_seconds=DEFAULT_VERIFICATION_POLL_POLICY.max_no_progress_seconds,
        max_consecutive_errors=DEFAULT_VERIFICATION_POLL_POLICY.max_consecutive_errors,
    )
    request_contexts = {
        custom_id: {
            "finding_idx": meta["finding_idx"],
            "original_prompt": _build_verification_prompt(findings[meta["finding_idx"]], cycle=cycle),
        }
        for custom_id, meta in job.request_map.items()
    }
    current_job = job
    for wave_index in range(max_waves):
        wave_label = f"wave {wave_index + 1}/{max_waves}"
        log(f"Verification {wave_label}: polling batch {current_job.batch_id}...")
        poll_outcome = poll_batch_bounded(
            current_job.batch_id,
            policy=policy,
            log=log,
            progress_cb=lambda status: progress(5.0 + (status.progress_pct / 100.0) * 85.0, f"Verification {wave_label}: {status.completed}/{status.total} done"),
        )
        if poll_outcome.detached or poll_outcome.poll_failed:
            log(f"Verification {wave_label}: polling ended before terminal status. Remaining findings will be marked UNVERIFIED.")
            break
        active_contexts = {cid: ctx for cid, ctx in request_contexts.items() if ctx.get("resolved") is not True}
        outcomes = _classify_wave_results(job=current_job, findings=findings, request_contexts=active_contexts)
        needs_retry: list[VerificationItemOutcome] = []
        needs_continue: list[VerificationItemOutcome] = []
        terminal_unverified = 0
        succeeded = 0
        for outcome in outcomes:
            finding = findings[outcome.finding_idx]
            if outcome.classification == "success" and outcome.parsed_verification:
                finding.verification = outcome.parsed_verification
                request_contexts[outcome.original_custom_id]["resolved"] = True
                succeeded += 1
            elif outcome.classification == "retry":
                needs_retry.append(outcome)
            elif outcome.classification == "continue":
                needs_continue.append(outcome)
            else:
                finding.verification = VerificationResult(verdict="UNVERIFIED", explanation=outcome.unverified_reason or "Verification failed.")
                request_contexts[outcome.original_custom_id]["resolved"] = True
                terminal_unverified += 1
        log(f"Verification {wave_label} results: {succeeded} succeeded, {len(needs_continue)} need continuation, {len(needs_retry)} need retry, {terminal_unverified} terminal UNVERIFIED")
        if not needs_retry and not needs_continue:
            break
        if wave_index == max_waves - 1:
            for outcome in needs_retry + needs_continue:
                finding = findings[outcome.finding_idx]
                finding.verification = VerificationResult(verdict="UNVERIFIED", explanation=f"Verification unresolved after {max_waves} batch waves: {outcome.unverified_reason or outcome.classification}.")
            break
        next_requests = []
        next_request_map = {}
        next_contexts: dict[str, dict] = {}
        for item in needs_retry:
            original = request_contexts[item.original_custom_id]
            custom_id = f"verify_retry_{wave_index + 1}__{item.original_custom_id}"
            next_requests.append({"custom_id": custom_id, "params": _build_retry_request(original["original_prompt"], cycle=cycle)})
            next_request_map[custom_id] = {"finding_idx": item.finding_idx, "wave": wave_index + 2, "type": "retry"}
            next_contexts[custom_id] = {"finding_idx": item.finding_idx, "original_prompt": original["original_prompt"], "resolved": False}
        for item in needs_continue:
            original = request_contexts[item.original_custom_id]
            custom_id = f"verify_cont_{wave_index + 1}__{item.original_custom_id}"
            next_requests.append({"custom_id": custom_id, "params": _build_continuation_request(original["original_prompt"], item.assistant_content_blocks or [], cycle=cycle)})
            next_request_map[custom_id] = {"finding_idx": item.finding_idx, "wave": wave_index + 2, "type": "continuation"}
            next_contexts[custom_id] = {"finding_idx": item.finding_idx, "original_prompt": original["original_prompt"], "resolved": False}
        log(f"Verification wave {wave_index + 2} submitting: {len(needs_retry)} retries, {len(needs_continue)} continuations")
        current_job = submit_verification_followup_wave(next_requests, next_request_map)
        request_contexts = next_contexts
    counts = {"CONFIRMED": 0, "CORRECTED": 0, "DISPUTED": 0, "UNVERIFIED": 0}
    for finding in findings:
        if finding.verification is None:
            finding.verification = VerificationResult(verdict="UNVERIFIED", explanation="No verification result after all batch waves.")
        counts[finding.verification.verdict] = counts.get(finding.verification.verdict, 0) + 1
    log(
        "Verification complete: "
        f"{counts.get('CONFIRMED', 0)} confirmed, "
        f"{counts.get('CORRECTED', 0)} corrected, "
        f"{counts.get('DISPUTED', 0)} disputed, "
        f"{counts.get('UNVERIFIED', 0)} unverified"
    )
    return findings
