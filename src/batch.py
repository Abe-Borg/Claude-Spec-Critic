"""
Batch processing for Spec Critic using the Anthropic Message Batches API.

Handles submission, polling, and result retrieval for batched spec reviews
and finding verifications. Provides 50% cost savings over real-time API calls
at the cost of asynchronous processing (typically under 1 hour).

v2.3.0 — Opus-only. All batch stages use Opus 4.6.

v1.7.0 — Verification batching + model selection.

Usage:
    from batch import submit_review_batch, poll_batch, retrieve_review_results

    job = submit_review_batch(specs, project_context="New elementary school")

    while True:
        status = poll_batch(job.batch_id)
        if status["status"] == "ended":
            break
        time.sleep(10)

    results = retrieve_review_results(job)
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, Optional

from anthropic import Anthropic

from .prompts import get_system_prompt, get_single_spec_user_message
from .reviewer import (
    Finding,
    ReviewResult,
    _extract_json_array,
    _parse_findings,
    _get_api_key,
    MODEL_OPUS_46,
)


@dataclass
class BatchJob:
    """Tracks a batch submission and its metadata."""
    batch_id: str
    job_type: str               # "review" or "verify"
    request_map: dict           # custom_id -> metadata
    created_at: float
    status: str = "submitted"


@dataclass
class BatchStatus:
    """Snapshot of batch processing status."""
    status: str
    processing: int
    succeeded: int
    errored: int
    canceled: int
    expired: int
    total: int

    @property
    def completed(self) -> int:
        return self.succeeded + self.errored + self.canceled + self.expired

    @property
    def progress_pct(self) -> float:
        return (self.completed / self.total * 100) if self.total > 0 else 0.0


def _sanitize_custom_id(filename: str, max_len: int = 50) -> str:
    stem = Path(filename).stem if "." in filename else filename
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", stem)
    return safe[:max_len]


# ---------------------------------------------------------------------------
# Review batch
# ---------------------------------------------------------------------------

def submit_review_batch(
    specs: list,
    *,
    project_context: str = "",
    model: str = MODEL_OPUS_46,
) -> BatchJob:
    """Submit all spec reviews as a single Anthropic Message Batch."""
    if not specs:
        raise ValueError("No specs to submit for batch review")

    client = Anthropic(api_key=_get_api_key())
    system_prompt = get_system_prompt()

    batch_requests = []
    request_map = {}

    for idx, spec in enumerate(specs):
        safe_name = _sanitize_custom_id(spec.filename)
        custom_id = f"review__{safe_name}__{idx}"

        user_message = get_single_spec_user_message(
            spec.content,
            spec.filename,
            project_context=project_context,
        )

        batch_requests.append({
            "custom_id": custom_id,
            "params": {
                "model": model,
                "max_tokens": 32768,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            },
        })

        request_map[custom_id] = {
            "filename": spec.filename,
            "index": idx,
            "type": "review",
        }

    message_batch = client.messages.batches.create(requests=batch_requests)

    return BatchJob(
        batch_id=message_batch.id,
        job_type="review",
        request_map=request_map,
        created_at=time.time(),
    )


def poll_batch(batch_id: str) -> BatchStatus:
    """Check batch processing status."""
    client = Anthropic(api_key=_get_api_key())
    batch = client.messages.batches.retrieve(batch_id)
    counts = batch.request_counts

    return BatchStatus(
        status=batch.processing_status,
        processing=counts.processing,
        succeeded=counts.succeeded,
        errored=counts.errored,
        canceled=counts.canceled,
        expired=counts.expired,
        total=(
            counts.processing + counts.succeeded + counts.errored
            + counts.canceled + counts.expired
        ),
    )


def retrieve_review_results(job: BatchJob, *, model: str) -> dict[str, ReviewResult]:
    """Retrieve and parse review results from a completed batch."""
    client = Anthropic(api_key=_get_api_key())
    results_by_request: dict[str, ReviewResult] = {}

    for result in client.messages.batches.results(job.batch_id):
        custom_id = result.custom_id
        meta = job.request_map.get(custom_id)
        if not meta:
            continue

        if result.result.type == "succeeded":
            message = result.result.message

            response_text = ""
            for block in message.content:
                if hasattr(block, "text"):
                    response_text += block.text

            usage = message.usage if hasattr(message, "usage") else None
            input_tokens = getattr(usage, "input_tokens", 0) if usage else 0
            output_tokens = getattr(usage, "output_tokens", 0) if usage else 0

            try:
                data, thinking = _extract_json_array(response_text)
                findings = _parse_findings(data)
            except Exception as e:
                results_by_request[custom_id] = ReviewResult(
                    findings=[],
                    raw_response=response_text,
                    thinking=response_text,
                    model=model,
                    input_tokens=int(input_tokens or 0),
                    output_tokens=int(output_tokens or 0),
                    error=f"Failed to parse review output: {e}",
                )
                continue

            results_by_request[custom_id] = ReviewResult(
                findings=findings,
                raw_response=response_text,
                thinking=thinking,
                model=model,
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
            )
        else:
            error_msg = f"Batch request {result.result.type}"
            if hasattr(result.result, "error") and result.result.error:
                error_msg += f": {result.result.error}"

            results_by_request[custom_id] = ReviewResult(
                findings=[],
                error=error_msg,
            )

    return results_by_request


# ---------------------------------------------------------------------------
# Verification batch (v2.3.0: uses Opus 4.6)
# ---------------------------------------------------------------------------

def submit_verification_batch(
    findings: list[Finding],
    build_prompt_fn,
) -> BatchJob:
    """Submit verification requests as a single Anthropic Message Batch.

    Each finding gets its own batch request using Opus 4.6 with the
    web_search tool.
    """
    verifiable: list[tuple[int, Finding]] = []
    for i, f in enumerate(findings):
        verifiable.append((i, f))

    if not verifiable:
        raise ValueError("No findings eligible for verification")

    verifiable.sort(key=lambda pair: pair[1].confidence)

    client = Anthropic(api_key=_get_api_key())

    batch_requests = []
    request_map = {}

    for batch_idx, (finding_idx, finding) in enumerate(verifiable):
        custom_id = f"verify__{batch_idx}"
        prompt = build_prompt_fn(finding)

        batch_requests.append({
            "custom_id": custom_id,
            "params": {
                "model": MODEL_OPUS_46,
                "max_tokens": 1024,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}],
            },
        })

        request_map[custom_id] = {
            "batch_idx": batch_idx,
            "finding_idx": finding_idx,
        }

    message_batch = client.messages.batches.create(requests=batch_requests)

    return BatchJob(
        batch_id=message_batch.id,
        job_type="verify",
        request_map=request_map,
        created_at=time.time(),
    )


def retrieve_verification_results(
    job: BatchJob,
    findings: list[Finding],
    parse_response_fn,
) -> list[Finding]:
    """Retrieve verification results from a completed batch and populate findings."""
    from .verifier import VerificationResult

    client = Anthropic(api_key=_get_api_key())

    for result in client.messages.batches.results(job.batch_id):
        custom_id = result.custom_id
        meta = job.request_map.get(custom_id)
        if not meta:
            continue

        finding_idx = meta["finding_idx"]
        if finding_idx < 0 or finding_idx >= len(findings):
            continue

        finding = findings[finding_idx]

        if result.result.type == "succeeded":
            message = result.result.message

            response_text = ""
            for block in message.content:
                if hasattr(block, "text"):
                    response_text += block.text

            if response_text:
                finding.verification = parse_response_fn(response_text)
            else:
                finding.verification = VerificationResult(
                    verdict="UNVERIFIED",
                    explanation="Verification produced no text response.",
                )
        else:
            error_msg = f"Batch verification {result.result.type}"
            if hasattr(result.result, "error") and result.result.error:
                error_msg += f": {result.result.error}"

            finding.verification = VerificationResult(
                verdict="UNVERIFIED",
                explanation=f"Verification failed: {error_msg}",
            )

    for f in findings:
        if f.verification is None:
            f.verification = VerificationResult(
                verdict="UNVERIFIED",
                explanation="No verification result returned from batch.",
            )

    return findings


def cancel_batch(batch_id: str) -> str:
    """Cancel a running batch."""
    client = Anthropic(api_key=_get_api_key())
    batch = client.messages.batches.cancel(batch_id)
    return batch.processing_status