"""Batch processing for Spec Critic using Anthropic Message Batches API."""

from __future__ import annotations

import re
import time
from pathlib import Path
from dataclasses import dataclass

from .prompts import get_system_prompt, get_single_spec_user_message
from .reviewer import Finding, ReviewResult, _extract_json_array, _parse_findings, _get_client, MODEL_OPUS_46
from .code_cycles import CodeCycle, DEFAULT_CYCLE


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


def submit_review_batch(specs: list, *, project_context: str = "", model: str = MODEL_OPUS_46, cycle: CodeCycle = DEFAULT_CYCLE) -> BatchJob:
    if not specs:
        raise ValueError("No specs to submit for batch review")
    client = _get_client()
    system_prompt = get_system_prompt(cycle)
    batch_requests = []
    request_map = {}
    for idx, spec in enumerate(specs):
        custom_id = f"review__{_sanitize_custom_id(spec.filename)}__{idx}"
        user_message = get_single_spec_user_message(spec.content, spec.filename, project_context=project_context, cycle=cycle)
        batch_requests.append({"custom_id": custom_id, "params": {"model": model, "max_tokens": 32768, "system": system_prompt, "messages": [{"role": "user", "content": user_message}]}})
        request_map[custom_id] = {"filename": spec.filename, "index": idx, "type": "review"}
    mb = client.messages.batches.create(requests=batch_requests)
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
        response_text = "".join(block.text for block in message.content if hasattr(block, "text"))
        usage = message.usage if hasattr(message, "usage") else None
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        stop_reason = getattr(message, "stop_reason", None)

        if stop_reason != "end_turn":
            results[custom_id] = ReviewResult(findings=[], raw_response=response_text, stop_reason=stop_reason, parse_status="incomplete", model=model, input_tokens=input_tokens, output_tokens=output_tokens, error=f"Batch response incomplete (stop_reason: {stop_reason})")
            continue
        try:
            data, thinking = _extract_json_array(response_text, stop_reason=stop_reason)
            findings = _parse_findings(data)
            results[custom_id] = ReviewResult(findings=findings, raw_response=response_text, thinking=thinking, model=model, input_tokens=input_tokens, output_tokens=output_tokens, stop_reason=stop_reason, parse_status="ok")
        except Exception as e:
            results[custom_id] = ReviewResult(findings=[], raw_response=response_text, thinking=response_text, model=model, input_tokens=input_tokens, output_tokens=output_tokens, stop_reason=stop_reason, parse_status="parse_error", error=f"Failed to parse review output: {e}")
    return results


def submit_verification_batch(findings: list[Finding], build_prompt_fn) -> BatchJob:
    if not findings:
        raise ValueError("No findings eligible for verification")
    verifiable = list(enumerate(findings))
    verifiable.sort(key=lambda pair: pair[1].confidence)
    client = _get_client()
    reqs = []
    request_map = {}
    for batch_idx, (finding_idx, finding) in enumerate(verifiable):
        custom_id = f"verify__{batch_idx}"
        reqs.append({"custom_id": custom_id, "params": {"model": MODEL_OPUS_46, "max_tokens": 1024, "tools": [{"type": "web_search_20250305", "name": "web_search"}], "messages": [{"role": "user", "content": build_prompt_fn(finding)}]}})
        request_map[custom_id] = {"batch_idx": batch_idx, "finding_idx": finding_idx}
    mb = client.messages.batches.create(requests=reqs)
    return BatchJob(batch_id=mb.id, job_type="verify", request_map=request_map, created_at=time.time())


def retrieve_verification_results(job: BatchJob, findings: list[Finding], parse_response_fn) -> list[Finding]:
    from .verifier import VerificationResult
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
            finding.verification = VerificationResult(verdict="UNVERIFIED", explanation=f"Verification failed: {result.result.type}")
            continue
        message = result.result.message
        stop_reason = getattr(message, "stop_reason", None)
        if stop_reason != "end_turn":
            finding.verification = VerificationResult(verdict="UNVERIFIED", explanation=f"Verification response incomplete (stop_reason: {stop_reason}).")
            continue

        response_text = ""
        search_urls: list[str] = []
        for block in message.content:
            if hasattr(block, "text"):
                response_text += block.text
            if getattr(block, "type", None) == "web_search_tool_result":
                for r in (getattr(block, "results", []) or []):
                    url = getattr(r, "url", None)
                    if url:
                        search_urls.append(url)

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


def cancel_batch(batch_id: str) -> str:
    client = _get_client()
    batch = client.messages.batches.cancel(batch_id)
    return batch.processing_status
