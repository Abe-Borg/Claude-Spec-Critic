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
from .api_config import (
    MODEL_OPUS_46,
    VERIFICATION_ESCALATION_MODEL,
    VERIFICATION_MODEL_DEFAULT as VERIFICATION_MODEL,
    extract_cache_usage,
    system_prompt_with_cache,
    tools_with_cache,
    verification_max_tokens,
    WEB_SEARCH_TOOL,
)
from .verification_cache import VerificationCache
from .verification_router import (
    classify_finding_for_verification,
    escalation_verification_model,
    initial_verification_model,
    local_skip_enabled,
    should_escalate_verification,
)

# VERIFICATION_MAX_TOKENS is computed once at import for backward-compat
# with callers that read the constant. The dynamic helper is used for the
# request shape so future model routing (Phase 3) can change it per call.
VERIFICATION_MAX_TOKENS = verification_max_tokens()

VerifyProgressFn = Callable[[int, int, str], None]
MAX_VERIFICATION_WAVES = 3
_ERRORED_RETRY_MAX = 75  # Backward-compatibility constant for existing tests/imports.

# Phase 3 (plan 7.4): when a batch run finishes with only a few unresolved
# items, optionally fall back to real-time verification for the remainder
# instead of submitting another batch wave.
_REALTIME_FALLBACK_THRESHOLD = int(
    os.environ.get("SPEC_CRITIC_REALTIME_FALLBACK_THRESHOLD", "0")
)


def _noop_verify_progress(_: int, __: int, ___: str) -> None:
    return


@dataclass
class VerificationResult:
    verdict: str
    explanation: str = ""
    sources: list[str] = field(default_factory=list)
    correction: str | None = None
    # ----- Phase 3 evidence model (plan 7.5) -------------------------------
    # ``grounded`` records whether the verdict was backed by at least one
    # successful web_search_result block. The verifier production paths
    # never mark a result CONFIRMED/CORRECTED unless this is True.
    grounded: bool = False
    model_used: str = ""
    escalated: bool = False
    # "n/a" — not part of a cache-aware run
    # "miss" — verifier ran fresh and produced this result
    # "hit"  — result reused from a previous finding in the same run
    # "local_skip" — finding was diagnosed locally; no web verification ran
    cache_status: str = "n/a"
    web_search_requests: int = 0
    successful_source_count: int = 0
    search_error_count: int = 0


def _enforce_grounding_invariant(result: VerificationResult) -> VerificationResult:
    """Downgrade verified-but-ungrounded verdicts to UNVERIFIED.

    Plan 7.5 acceptance: a result cannot be marked verified if ``grounded``
    is False. Locally-skipped findings are exempt — they are explicitly
    UNVERIFIED already and never claim CONFIRMED.
    """
    verdict = (result.verdict or "").strip().upper()
    if verdict in ("CONFIRMED", "CORRECTED") and not result.grounded:
        result.verdict = "UNVERIFIED"
        suffix = " (downgraded: verdict lacked external grounding)"
        if not result.explanation:
            result.explanation = "Verdict downgraded to UNVERIFIED: no external evidence."
        elif suffix not in result.explanation:
            result.explanation = (result.explanation + suffix)[:500]
    return result


def _local_skip_result(reason: str = "Locally classified: external grounding not required for this finding.") -> VerificationResult:
    return VerificationResult(
        verdict="UNVERIFIED",
        explanation=reason,
        grounded=False,
        cache_status="local_skip",
        model_used="local",
    )


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
        "- The only tool available is web_search. Render the verdict from the",
        "  evidence it returns; do not fabricate a tool that has not been provided.",
        "- If continuing from a paused turn, finish pending work instead of restarting from scratch.",
    ])


def _content_block_to_plain(block) -> dict | None:
    """Best-effort convert an Anthropic SDK content block to a plain dict.

    Storing live SDK Pydantic objects in continuation state ties our resume
    flow to a specific SDK shape; converting at capture time (audit Issue 8)
    decouples it. ``maybe_transform`` accepts plain dicts or Pydantic models
    on the way out, so either form works downstream.
    """
    if block is None:
        return None
    if isinstance(block, dict):
        return block
    dumper = getattr(block, "model_dump", None)
    if callable(dumper):
        try:
            data = dumper(mode="python", exclude_none=False)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    legacy_dumper = getattr(block, "dict", None)
    if callable(legacy_dumper):
        try:
            data = legacy_dumper()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    block_type = getattr(block, "type", None)
    if not block_type:
        return None
    fallback: dict = {"type": str(block_type)}
    for attr in ("text", "id", "name", "input", "content", "tool_use_id", "results"):
        if hasattr(block, attr):
            value = getattr(block, attr)
            if value is not None:
                fallback[attr] = value
    return fallback


def _collect_search_evidence(message) -> tuple[list[str], int, int]:
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
            if isinstance(block_content, list):
                # Only count this block as a successful search if it contains
                # at least one usable web_search_result item. Error-only lists
                # must not count as success — that would let verdicts pass the
                # external-grounding gate without any real evidence.
                block_had_valid_result = False
                for item in block_content:
                    item_type = getattr(item, "type", None)
                    if item_type == "web_search_tool_result_error":
                        error_count += 1
                        continue
                    if item_type not in (None, "web_search_result"):
                        continue
                    block_had_valid_result = True
                    url = getattr(item, "url", None)
                    if url:
                        search_urls.append(url)
                if block_had_valid_result:
                    success_count += 1
            elif getattr(block_content, "type", None) == "web_search_tool_result_error":
                # Anthropic SDK models this as a union:
                # WebSearchToolResultBlock.content can be a WebSearchToolResultError object.
                error_count += 1
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
    if web_search_count > 0 and success_count > 0:
        return None
    if error_count > 0 and success_count == 0:
        return f"Web search attempted but all {error_count} search requests failed."
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


def verify_finding(
    finding: Finding,
    *,
    max_retries: int = 2,
    cycle: CodeCycle = DEFAULT_CYCLE,
    model: str | None = None,
    cache: VerificationCache | None = None,
    escalated: bool = False,
) -> VerificationResult:
    """Verify a single finding using Claude with web search.

    Uses the streaming API because the web_search_20250305 server tool
    requires streaming — non-streaming messages.create() will fail with
    a "streaming is required" error when server-side tools are active.

    Adaptive thinking is enabled so the model can reason through complex
    code-reference chains before rendering a verdict.

    Phase 3:
    - ``model`` overrides the default verifier (Sonnet/Opus routing).
    - ``cache`` short-circuits for findings that match a previously verified
      claim in the same run.
    - ``escalated`` is propagated into the result so diagnostics can
      distinguish the first pass from the Opus retry.
    """
    if cache is not None:
        cached = cache.get(finding, cycle=cycle)
        if cached is not None:
            return cached

    if local_skip_enabled() and classify_finding_for_verification(finding) == "local_skip":
        return _local_skip_result()

    selected_model = model or initial_verification_model()
    result = _run_verification_call(
        finding,
        cycle=cycle,
        model=selected_model,
        max_retries=max_retries,
        escalated=escalated,
    )

    # Escalation: re-run on Opus when Sonnet failed to ground a high-stakes
    # finding. Skip when caller already passed escalated=True (avoid loops)
    # or the routing config has nowhere to escalate to.
    if not escalated and should_escalate_verification(
        finding,
        verdict=result.verdict,
        grounded=result.grounded,
        successful_source_count=result.successful_source_count,
        search_error_count=result.search_error_count,
    ):
        escalated_model = escalation_verification_model()
        if escalated_model and escalated_model != selected_model:
            esc_result = _run_verification_call(
                finding,
                cycle=cycle,
                model=escalated_model,
                max_retries=max_retries,
                escalated=True,
            )
            # Prefer the escalated result when it produced a grounded verdict;
            # otherwise keep the first pass so we don't lose its evidence.
            if esc_result.grounded or (
                esc_result.verdict in ("CONFIRMED", "CORRECTED", "DISPUTED")
                and result.verdict == "UNVERIFIED"
            ):
                result = esc_result

    if cache is not None and result.cache_status == "miss":
        cache.put(finding, cycle=cycle, result=result)
    return result


def _run_verification_call(
    finding: Finding,
    *,
    cycle: CodeCycle,
    model: str,
    max_retries: int,
    escalated: bool,
) -> VerificationResult:
    """Single verification call (no caching, no escalation).

    Always returns a VerificationResult with the Phase 3 evidence fields
    populated (``model_used``, ``grounded``, ``escalated``, search counts).
    """
    def _make_unverified(explanation: str, *, search_requests: int = 0, search_errors: int = 0, search_successes: int = 0) -> VerificationResult:
        return _enforce_grounding_invariant(VerificationResult(
            verdict="UNVERIFIED",
            explanation=explanation,
            grounded=False,
            model_used=model,
            escalated=escalated,
            cache_status="miss",
            web_search_requests=search_requests,
            successful_source_count=search_successes,
            search_error_count=search_errors,
        ))

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _make_unverified("No API key available for verification.")

    client = _get_client()
    prompt = _build_verification_prompt(finding, cycle=cycle)
    system_prompt = _get_verification_system_prompt(cycle)
    system_payload = system_prompt_with_cache(system_prompt)
    tools_payload = tools_with_cache([WEB_SEARCH_TOOL])
    output_limit = verification_max_tokens(model=model)

    for attempt in range(max_retries + 1):
        try:
            all_responses = []
            messages = [{"role": "user", "content": prompt}]
            max_continuations = 10
            for _ in range(max_continuations + 1):
                # --- Streaming API required for web search server tool ---
                with client.messages.stream(
                    model=model,
                    max_tokens=output_limit,
                    thinking={"type": "adaptive"},
                    system=system_payload,
                    tools=tools_payload,
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
                return _make_unverified(f"Verification response incomplete (stop_reason: {stop_reason}).")
            if not all_responses or getattr(all_responses[-1], "stop_reason", None) != "end_turn":
                return _make_unverified("Verification did not complete after maximum continuation attempts.")

            response_text = ""
            all_search_urls: list[str] = []
            success_blocks = 0
            total_search_errors = 0
            total_search_requests = 0
            for resp in all_responses:
                for block in getattr(resp, "content", []) or []:
                    if hasattr(block, "text") and block.text is not None:
                        response_text += block.text

                search_urls, successes, errors = _collect_search_evidence(resp)
                all_search_urls.extend(search_urls)
                success_blocks += successes
                total_search_errors += errors
                total_search_requests += _web_search_count(resp)

            grounded = success_blocks > 0
            if not grounded:
                if total_search_errors > 0:
                    return _make_unverified(
                        f"Web search attempted but all {total_search_errors} search requests failed.",
                        search_requests=total_search_requests,
                        search_errors=total_search_errors,
                    )
                return _make_unverified(
                    "Verification did not perform web search. Verdict requires external grounding.",
                    search_requests=total_search_requests,
                    search_errors=total_search_errors,
                )

            if not response_text.strip():
                return _make_unverified(
                    "Verification produced no text response.",
                    search_requests=total_search_requests,
                    search_errors=total_search_errors,
                    search_successes=success_blocks,
                )

            parsed = _parse_verification_response(response_text)
            if all_search_urls:
                existing = set(parsed.sources)
                for url in all_search_urls:
                    if url not in existing:
                        parsed.sources.append(url)
            parsed.grounded = True
            parsed.model_used = model
            parsed.escalated = escalated
            parsed.cache_status = "miss"
            parsed.web_search_requests = total_search_requests
            parsed.successful_source_count = len(all_search_urls)
            parsed.search_error_count = total_search_errors
            return _enforce_grounding_invariant(parsed)
        except RateLimitError:
            if attempt < max_retries:
                time.sleep(10 * (attempt + 1))
                continue
            return _make_unverified("Rate limited during verification.")
        except InternalServerError as e:
            if attempt < max_retries:
                time.sleep(15 * (attempt + 1))
                continue
            return _make_unverified(f"Server overloaded during verification: {e}")
        except APIStatusError as e:
            if getattr(e, "status_code", None) == 529 or e.__class__.__name__ == "OverloadedError":
                if attempt < max_retries:
                    time.sleep(15 * (attempt + 1))
                    continue
                return _make_unverified(f"Server overloaded during verification: {e}")
            if attempt < max_retries:
                time.sleep(5 * (attempt + 1))
                continue
            return _make_unverified(f"API error during verification: {e}")
        except (APIConnectionError, APIError) as e:
            if attempt < max_retries:
                time.sleep(5 * (attempt + 1))
                continue
            return _make_unverified(f"API error during verification: {e}")
        except Exception as e:
            return _make_unverified(f"Unexpected error during verification: {e}")


def prepare_findings_for_verification(
    findings: list[Finding],
    *,
    cycle: CodeCycle = DEFAULT_CYCLE,
    cache: VerificationCache | None = None,
    log: Callable[..., None] = lambda *_a, **_k: None,
) -> list[Finding]:
    """Apply Phase 3 pre-pass: local skip + cache lookup.

    Mutates ``findings`` in place — any finding that resolves locally
    (local-skip classification or cache hit) gets ``f.verification`` set
    here. Returns the subset of findings that still need a remote
    verification call.
    """
    remaining: list[Finding] = []
    skipped_local = 0
    cache_hits = 0
    for f in findings:
        if local_skip_enabled() and classify_finding_for_verification(f) == "local_skip":
            f.verification = _local_skip_result()
            skipped_local += 1
            continue
        if cache is not None:
            cached = cache.get(f, cycle=cycle)
            if cached is not None:
                f.verification = cached
                cache_hits += 1
                continue
        remaining.append(f)
    if skipped_local or cache_hits:
        log(
            f"Verification pre-pass: {skipped_local} locally skipped, "
            f"{cache_hits} cache hits, {len(remaining)} require web verification.",
            level="info",
        )
    return remaining


def verify_findings(findings: list[Finding], *, progress: VerifyProgressFn = _noop_verify_progress, cycle: CodeCycle = DEFAULT_CYCLE, cache: VerificationCache | None = None) -> list[Finding]:
    verifiable = list(findings)
    verifiable.sort(key=lambda f: f.confidence)
    # Resolve local-skip and cache-hit findings before spinning up workers.
    remaining = prepare_findings_for_verification(verifiable, cycle=cycle, cache=cache)
    total = len(remaining)
    if total == 0:
        return findings
    max_workers = min(5, total)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(verify_finding, f, cycle=cycle, cache=cache): f for f in remaining}
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


def verify_findings_batch(
    findings: list[Finding],
    *,
    log: Callable[..., None] = lambda *_a, **_k: None,
    progress: Callable[[float, str], None] = lambda _p, _m: None,
    poll_interval: int = 15,
    cycle: CodeCycle = DEFAULT_CYCLE,
    cache: VerificationCache | None = None,
) -> list[Finding]:
    if not findings:
        log("No findings eligible for batch verification.", level="info")
        return findings

    remaining = prepare_findings_for_verification(findings, cycle=cycle, cache=cache, log=log)
    if not remaining:
        progress(100.0, "Verification complete (all resolved locally / cached)")
        return findings

    progress(0.0, f"Submitting {len(remaining)} verification requests...")
    job = start_verification_batch(remaining, cycle=cycle)
    log(f"Verification batch submitted: {job.batch_id}", level="step")

    collect_verification_batch_results(job, remaining, log=log, progress=progress, poll_interval=poll_interval, cycle=cycle, cache=cache)
    progress(100.0, "Verification complete")
    return findings


def _retry_failed_verifications_realtime(
    findings: list[Finding],
    *,
    cycle: CodeCycle = DEFAULT_CYCLE,
    log: Callable[..., None] = lambda *_a, **_k: None,
    max_retry_count: int = _ERRORED_RETRY_MAX,
) -> None:
    """No-op retained for import compatibility.

    Previously retried UNVERIFIED findings via real-time streaming API.
    Removed in v2.8.0 as part of batch-only enforcement. Retained because
    external code or tests may import this symbol. Does nothing when called.

    Safe to delete once all downstream imports are confirmed updated.
    """
    pass


def start_verification_batch(findings: list[Finding], *, cycle: CodeCycle = DEFAULT_CYCLE, model: str | None = None) -> BatchJob:
    return submit_verification_batch(
        findings,
        build_prompt_fn=lambda finding: _build_verification_prompt(finding, cycle=cycle),
        system_prompt_fn=_get_verification_system_prompt,
        cycle=cycle,
        model=model or initial_verification_model(),
    )


def _build_retry_request(prompt: str, *, cycle: CodeCycle, model: str | None = None) -> dict:
    selected = model or initial_verification_model()
    return {
        "model": selected,
        "max_tokens": verification_max_tokens(model=selected),
        "thinking": {"type": "adaptive"},
        "system": system_prompt_with_cache(_get_verification_system_prompt(cycle)),
        "tools": tools_with_cache([WEB_SEARCH_TOOL]),
        "messages": [{"role": "user", "content": prompt}],
    }


def _build_continuation_request(prompt: str, assistant_content_blocks: list, *, cycle: CodeCycle, model: str | None = None) -> dict:
    selected = model or initial_verification_model()
    return {
        "model": selected,
        "max_tokens": verification_max_tokens(model=selected),
        "thinking": {"type": "adaptive"},
        "system": system_prompt_with_cache(_get_verification_system_prompt(cycle)),
        "tools": tools_with_cache([WEB_SEARCH_TOOL]),
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
        model_used = context.get("model") or job.request_map.get(custom_id, {}).get("model") or VERIFICATION_MODEL
        escalated = bool(context.get("escalated", False))
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
            raw_blocks = getattr(message, "content", []) or []
            plain_blocks = [b for b in (_content_block_to_plain(rb) for rb in raw_blocks) if b is not None]
            outcomes.append(
                VerificationItemOutcome(
                    finding_idx=finding_idx,
                    original_custom_id=custom_id,
                    classification="continue",
                    # Plain dicts decouple the continuation payload from SDK
                    # Pydantic shape changes (audit Issue 8). maybe_transform
                    # accepts these the same way it accepts model objects.
                    assistant_content_blocks=plain_blocks,
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
        search_urls, success_blocks, error_count = _collect_search_evidence(message)
        if search_urls:
            existing = set(parsed.sources)
            for url in search_urls:
                if url not in existing:
                    parsed.sources.append(url)
        # Phase 3 evidence model: stamp grounding/source counts so the
        # downstream invariant can downgrade ungrounded verified verdicts.
        parsed.grounded = success_blocks > 0
        parsed.model_used = model_used
        parsed.escalated = escalated
        parsed.cache_status = "miss"
        parsed.web_search_requests = _web_search_count(message)
        parsed.successful_source_count = len(search_urls)
        parsed.search_error_count = error_count
        parsed = _enforce_grounding_invariant(parsed)
        outcomes.append(VerificationItemOutcome(finding_idx=finding_idx, original_custom_id=custom_id, classification="success", parsed_verification=parsed))
    return outcomes


def collect_verification_batch_results(
    job: BatchJob,
    findings: list[Finding],
    *,
    log: Callable[..., None] = lambda *_a, **_k: None,
    progress: Callable[[float, str], None] = lambda _p, _m: None,
    poll_interval: int = 15,
    cycle: CodeCycle = DEFAULT_CYCLE,
    poll_policy: PollPolicy | None = None,
    max_waves: int = MAX_VERIFICATION_WAVES,
    cache: VerificationCache | None = None,
    realtime_fallback_threshold: int | None = None,
) -> list[Finding]:
    if not findings:
        return findings
    policy = poll_policy or PollPolicy(
        poll_interval_seconds=poll_interval,
        max_elapsed_seconds=DEFAULT_VERIFICATION_POLL_POLICY.max_elapsed_seconds,
        max_no_progress_seconds=DEFAULT_VERIFICATION_POLL_POLICY.max_no_progress_seconds,
        max_consecutive_errors=DEFAULT_VERIFICATION_POLL_POLICY.max_consecutive_errors,
        backoff_after_seconds=DEFAULT_VERIFICATION_POLL_POLICY.backoff_after_seconds,
        max_poll_interval_seconds=DEFAULT_VERIFICATION_POLL_POLICY.max_poll_interval_seconds,
    )
    fallback_threshold = (
        realtime_fallback_threshold
        if realtime_fallback_threshold is not None
        else _REALTIME_FALLBACK_THRESHOLD
    )
    request_contexts = {
        custom_id: {
            "finding_idx": meta["finding_idx"],
            "original_prompt": _build_verification_prompt(findings[meta["finding_idx"]], cycle=cycle),
            "model": meta.get("model") or initial_verification_model(),
            "escalated": False,
        }
        for custom_id, meta in job.request_map.items()
    }
    current_job = job
    for wave_index in range(max_waves):
        wave_label = f"wave {wave_index + 1}/{max_waves}"
        log(f"Verification {wave_label}: polling batch {current_job.batch_id}...", level="step")
        poll_outcome = poll_batch_bounded(
            current_job.batch_id,
            policy=policy,
            log=log,
            progress_cb=lambda status: progress(5.0 + (status.progress_pct / 100.0) * 85.0, f"Verification {wave_label}: {status.completed}/{status.total} done"),
        )
        if poll_outcome.detached or poll_outcome.poll_failed:
            log(f"Verification {wave_label}: polling ended before terminal status. Remaining findings will be marked UNVERIFIED.", level="warning")
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
                if cache is not None:
                    cache.put(finding, cycle=cycle, result=outcome.parsed_verification)
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
        wave_summary_level = "warning" if (len(needs_retry) or len(needs_continue) or terminal_unverified) else "info"
        log(
            f"Verification {wave_label} results: {succeeded} succeeded, {len(needs_continue)} need continuation, {len(needs_retry)} need retry, {terminal_unverified} terminal UNVERIFIED",
            level=wave_summary_level,
        )
        if not needs_retry and not needs_continue:
            break
        if wave_index == max_waves - 1:
            unresolved = needs_retry + needs_continue
            # Phase 3 (plan 7.4): if only a small tail remains, fall back to
            # real-time verification rather than waiting for another batch.
            if (
                fallback_threshold > 0
                and len(unresolved) <= fallback_threshold
            ):
                log(
                    f"Verification: real-time fallback for {len(unresolved)} "
                    f"unresolved finding(s) (threshold={fallback_threshold}).",
                    level="info",
                )
                for outcome in unresolved:
                    finding = findings[outcome.finding_idx]
                    try:
                        finding.verification = verify_finding(finding, cycle=cycle, cache=cache)
                    except Exception as e:
                        finding.verification = VerificationResult(
                            verdict="UNVERIFIED",
                            explanation=f"Real-time fallback verification failed: {e}",
                        )
                break
            for outcome in unresolved:
                finding = findings[outcome.finding_idx]
                finding.verification = VerificationResult(verdict="UNVERIFIED", explanation=f"Verification unresolved after {max_waves} batch waves: {outcome.unverified_reason or outcome.classification}.")
            break
        next_requests = []
        next_request_map = {}
        next_contexts: dict[str, dict] = {}
        for item in needs_retry:
            original = request_contexts[item.original_custom_id]
            wave_model = original.get("model") or initial_verification_model()
            custom_id = f"verify_retry_{wave_index + 1}__{item.original_custom_id}"
            next_requests.append({"custom_id": custom_id, "params": _build_retry_request(original["original_prompt"], cycle=cycle, model=wave_model)})
            next_request_map[custom_id] = {"finding_idx": item.finding_idx, "wave": wave_index + 2, "type": "retry", "model": wave_model}
            next_contexts[custom_id] = {"finding_idx": item.finding_idx, "original_prompt": original["original_prompt"], "resolved": False, "model": wave_model, "escalated": original.get("escalated", False)}
        for item in needs_continue:
            original = request_contexts[item.original_custom_id]
            wave_model = original.get("model") or initial_verification_model()
            custom_id = f"verify_cont_{wave_index + 1}__{item.original_custom_id}"
            next_requests.append({"custom_id": custom_id, "params": _build_continuation_request(original["original_prompt"], item.assistant_content_blocks or [], cycle=cycle, model=wave_model)})
            next_request_map[custom_id] = {"finding_idx": item.finding_idx, "wave": wave_index + 2, "type": "continuation", "model": wave_model}
            next_contexts[custom_id] = {"finding_idx": item.finding_idx, "original_prompt": original["original_prompt"], "resolved": False, "model": wave_model, "escalated": original.get("escalated", False)}
        log(f"Verification wave {wave_index + 2} submitting: {len(needs_retry)} retries, {len(needs_continue)} continuations", level="step")
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
        f"{counts.get('UNVERIFIED', 0)} unverified",
        level="success",
    )
    return findings
