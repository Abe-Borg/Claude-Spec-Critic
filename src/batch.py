"""Batch processing for Spec Critic using Anthropic Message Batches API."""

from __future__ import annotations

import re
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from .prompts import get_system_prompt, get_single_spec_user_message
from .reviewer import Finding, ReviewResult, _extract_json_array, _parse_findings, _get_client, MODEL_OPUS_46
from .code_cycles import CodeCycle, DEFAULT_CYCLE
from .tokenizer import MAX_OUTPUT_TOKENS_OPUS, MAX_OUTPUT_TOKENS_SONNET, count_tokens
from .api_config import (
    BATCH_MAX_OUTPUT_TOKENS,
    BATCH_OUTPUT_BETA,
    VERIFICATION_MODEL_DEFAULT as VERIFICATION_MODEL,
    WEB_SEARCH_TOOL,
    assert_extended_output_allowed,
    extract_cache_usage,
    review_max_tokens,
    system_prompt_with_cache,
    tools_with_cache,
    verification_max_tokens,
)


@dataclass
class BatchJob:
    batch_id: str
    job_type: str
    request_map: dict
    created_at: float
    status: str = "submitted"


@dataclass
class BatchStatus:
    status: str
    processing: int
    succeeded: int
    errored: int
    canceled: int
    expired: int
    total: int

    @property
    def completed(self) -> int: return self.succeeded + self.errored + self.canceled + self.expired
    @property
    def progress_pct(self) -> float: return (self.completed / self.total * 100) if self.total > 0 else 0.0


def _sanitize_custom_id(filename: str, max_len: int = 50) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", Path(filename).stem if "." in filename else filename)[:max_len]


def submit_review_batch(
    specs: list,
    *,
    project_context: str = "",
    model: str = MODEL_OPUS_46,
    cycle: CodeCycle = DEFAULT_CYCLE,
    retry_instruction: str | None = None,
) -> BatchJob:
    if not specs:
        raise ValueError("No specs to submit for batch review")
    client = _get_client()
    system_prompt = get_system_prompt(cycle)
    system_payload = system_prompt_with_cache(system_prompt)
    use_extended_output = model == MODEL_OPUS_46
    # Local token estimate sized so the per-spec dispatcher can pick the
    # right cap without an extra API call. Conservative; only used to
    # decide between standard and extended-output sizing.
    system_tokens = count_tokens(system_prompt)
    batch_requests = []
    request_map = {}
    for idx, spec in enumerate(specs):
        custom_id = f"review__{_sanitize_custom_id(spec.filename)}__{idx}"
        user_message = get_single_spec_user_message(spec.content, spec.filename, project_context=project_context, cycle=cycle)
        if retry_instruction:
            user_message += f"\n\n{retry_instruction}"
        approx_input_tokens = system_tokens + count_tokens(user_message)
        output_limit = review_max_tokens(
            batch=True,
            model=model,
            input_tokens=approx_input_tokens,
            allow_extended_output=use_extended_output,
        )
        # Fail-fast guard: 300k requires the batch beta header. Plan Sprint
        # 2 item 8 — never let a 300k request slip through without it.
        betas = [BATCH_OUTPUT_BETA] if hasattr(client, "beta") else None
        assert_extended_output_allowed(max_tokens=output_limit, betas=betas)
        batch_requests.append({"custom_id": custom_id, "params": {"model": model, "max_tokens": output_limit, "thinking": {"type": "adaptive"}, "system": system_payload, "messages": [{"role": "user", "content": user_message}]}})
        request_map[custom_id] = {"filename": spec.filename, "index": idx, "type": "review"}

    create_fn = client.beta.messages.batches.create if hasattr(client, "beta") else client.messages.batches.create
    kwargs = {"requests": batch_requests}
    if hasattr(client, "beta"):
        kwargs["betas"] = [BATCH_OUTPUT_BETA]
    mb = create_fn(**kwargs)
    return BatchJob(batch_id=mb.id, job_type="review", request_map=request_map, created_at=time.time())


def poll_batch(batch_id: str) -> BatchStatus:
    client = _get_client()
    batch = client.messages.batches.retrieve(batch_id)
    counts = batch.request_counts
    return BatchStatus(status=batch.processing_status, processing=counts.processing, succeeded=counts.succeeded, errored=counts.errored, canceled=counts.canceled, expired=counts.expired, total=(counts.processing + counts.succeeded + counts.errored + counts.canceled + counts.expired))


def retrieve_review_results(job: BatchJob, *, model: str) -> dict[str, ReviewResult]:
    client = _get_client()
    results: dict[str, ReviewResult] = {}
    for result in client.messages.batches.results(job.batch_id):
        custom_id = result.custom_id
        if custom_id not in job.request_map:
            continue
        if result.result.type != "succeeded":
            err = f"Batch request {result.result.type}"
            if hasattr(result.result, "error") and result.result.error:
                err += f": {result.result.error}"
            results[custom_id] = ReviewResult(findings=[], error=err)
            continue
        message = result.result.message
        response_text = "".join(block.text for block in message.content if hasattr(block, "text") and block.text is not None)
        usage = message.usage if hasattr(message, "usage") else None
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache = extract_cache_usage(usage)
        stop_reason = getattr(message, "stop_reason", None)

        if stop_reason != "end_turn":
            results[custom_id] = ReviewResult(
                findings=[], raw_response=response_text, stop_reason=stop_reason,
                parse_status="incomplete", model=model,
                input_tokens=input_tokens, output_tokens=output_tokens,
                cache_creation_input_tokens=cache["cache_creation_input_tokens"],
                cache_read_input_tokens=cache["cache_read_input_tokens"],
                error=f"Batch response incomplete (stop_reason: {stop_reason})",
            )
            continue
        try:
            data, thinking = _extract_json_array(response_text, stop_reason=stop_reason)
            findings = _parse_findings(data)
            results[custom_id] = ReviewResult(
                findings=findings, raw_response=response_text, thinking=thinking,
                model=model, input_tokens=input_tokens, output_tokens=output_tokens,
                cache_creation_input_tokens=cache["cache_creation_input_tokens"],
                cache_read_input_tokens=cache["cache_read_input_tokens"],
                stop_reason=stop_reason, parse_status="ok",
            )
        except Exception as e:
            results[custom_id] = ReviewResult(
                findings=[], raw_response=response_text, thinking=response_text,
                model=model, input_tokens=input_tokens, output_tokens=output_tokens,
                cache_creation_input_tokens=cache["cache_creation_input_tokens"],
                cache_read_input_tokens=cache["cache_read_input_tokens"],
                stop_reason=stop_reason, parse_status="parse_error",
                error=f"Failed to parse review output: {e}",
            )
    return results


def _extract_api_error_message(error_obj) -> str:
    """Extract a clean, human-readable error message from a batch error object.

    The Anthropic SDK returns ErrorResponse objects with nested structure.
    This extracts the useful message and discards the repr noise.
    """
    if error_obj is None:
        return ""
    # Try to get the nested error message
    if hasattr(error_obj, "error"):
        inner = error_obj.error
        if hasattr(inner, "message"):
            msg = str(inner.message)
            error_type = str(getattr(inner, "type", "")) or ""
            if error_type:
                return f"{error_type}: {msg}"
            return msg
    # Try direct message attribute
    if hasattr(error_obj, "message"):
        return str(error_obj.message)
    # Fall back to str, but truncate long reprs
    s = str(error_obj)
    return s[:200] if len(s) > 200 else s


def retrieve_verification_results(job: BatchJob, findings: list[Finding], parse_response_fn) -> list[Finding]:
    from .verifier import VerificationResult, _collect_search_evidence, _search_gate_failure
    client = _get_client()
    for result in client.messages.batches.results(job.batch_id):
        meta = job.request_map.get(result.custom_id)
        if not meta:
            continue
        idx = meta["finding_idx"]
        if idx < 0 or idx >= len(findings):
            continue
        finding = findings[idx]
        if result.result.type != "succeeded":
            # Extract clean error message instead of dumping raw ErrorResponse repr
            error_msg = _extract_api_error_message(
                getattr(result.result, "error", None)
            )
            status_type = result.result.type  # "errored", "expired", "canceled"
            explanation = f"Verification failed: batch request {status_type}"
            if error_msg:
                explanation += f" ({error_msg})"
            finding.verification = VerificationResult(verdict="UNVERIFIED", explanation=explanation)
            continue
        message = result.result.message
        stop_reason = getattr(message, "stop_reason", None)
        if stop_reason == "pause_turn":
            finding.verification = VerificationResult(verdict="UNVERIFIED", explanation="Verification returned pause_turn in batch mode; retry via real-time verification path.")
            continue
        if stop_reason != "end_turn":
            finding.verification = VerificationResult(verdict="UNVERIFIED", explanation=f"Verification response incomplete (stop_reason: {stop_reason}).")
            continue

        response_text = ""
        for block in message.content:
            if hasattr(block, "text"):
                response_text += block.text
        search_gate_failure = _search_gate_failure(message)
        if search_gate_failure:
            finding.verification = VerificationResult(verdict="UNVERIFIED", explanation=search_gate_failure)
            continue
        search_urls, _, _ = _collect_search_evidence(message)

        if response_text.strip():
            parsed = parse_response_fn(response_text)
            if search_urls:
                existing = set(parsed.sources)
                for url in search_urls:
                    if url not in existing:
                        parsed.sources.append(url)
            finding.verification = parsed
        else:
            finding.verification = VerificationResult(verdict="UNVERIFIED", explanation="Verification produced no text response.")

    for f in findings:
        if f.verification is None:
            f.verification = VerificationResult(verdict="UNVERIFIED", explanation="No verification result returned from batch.")
    return findings


def retrieve_verification_results_detailed(job: BatchJob) -> dict[str, Any]:
    client = _get_client()
    results: dict[str, Any] = {}
    for result in client.messages.batches.results(job.batch_id):
        if result.custom_id not in job.request_map:
            continue
        results[result.custom_id] = result
    return results


def _build_verification_request_params(
    *,
    prompt: str,
    system_prompt: str,
    assistant_content: list | None = None,
    continue_turn: bool = False,
    model: str | None = None,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    if assistant_content is not None:
        messages.append({"role": "assistant", "content": assistant_content})
    if continue_turn:
        messages.append({"role": "user", "content": [{"type": "text", "text": "continue"}]})
    selected_model = model or VERIFICATION_MODEL
    return {
        "model": selected_model,
        "max_tokens": verification_max_tokens(model=selected_model),
        "thinking": {"type": "adaptive"},
        "system": system_prompt_with_cache(system_prompt),
        "tools": tools_with_cache([WEB_SEARCH_TOOL]),
        "messages": messages,
    }


def submit_verification_batch(
    findings: list[Finding],
    build_prompt_fn,
    system_prompt_fn,
    *,
    cycle: CodeCycle = DEFAULT_CYCLE,
    model: str | None = None,
) -> BatchJob:
    if not findings:
        raise ValueError("No findings eligible for verification")
    verifiable = list(enumerate(findings))
    verifiable.sort(key=lambda pair: pair[1].confidence)
    client = _get_client()
    reqs = []
    request_map = {}
    for batch_idx, (finding_idx, finding) in enumerate(verifiable):
        custom_id = f"verify__{batch_idx}"
        reqs.append(
            {
                "custom_id": custom_id,
                "params": _build_verification_request_params(
                    prompt=build_prompt_fn(finding),
                    system_prompt=system_prompt_fn(cycle),
                    model=model,
                ),
            }
        )
        request_map[custom_id] = {"batch_idx": batch_idx, "finding_idx": finding_idx, "model": model or VERIFICATION_MODEL}

    create_fn = client.beta.messages.batches.create if hasattr(client, "beta") else client.messages.batches.create
    kwargs = {"requests": reqs}
    if hasattr(client, "beta"):
        kwargs["betas"] = [BATCH_OUTPUT_BETA]
    mb = create_fn(**kwargs)

    return BatchJob(batch_id=mb.id, job_type="verify", request_map=request_map, created_at=time.time())


def submit_verification_followup_wave(
    requests: list[dict[str, Any]],
    request_map: dict[str, Any],
) -> BatchJob:
    if not requests:
        raise ValueError("No verification follow-up requests to submit")
    client = _get_client()
    create_fn = client.beta.messages.batches.create if hasattr(client, "beta") else client.messages.batches.create
    kwargs = {"requests": requests}
    if hasattr(client, "beta"):
        kwargs["betas"] = [BATCH_OUTPUT_BETA]
    mb = create_fn(**kwargs)
    return BatchJob(batch_id=mb.id, job_type="verify", request_map=request_map, created_at=time.time())


def cancel_batch(batch_id: str) -> str:
    client = _get_client()
    batch = client.messages.batches.cancel(batch_id)
    return batch.processing_status
