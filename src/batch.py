"""
Batch processing for Spec Critic using the Anthropic Message Batches API.

Handles submission, polling, and result retrieval for batched spec reviews
and finding verifications. Provides 50% cost savings over real-time API calls
at the cost of asynchronous processing (typically under 1 hour).

The batch pipeline supports two types of requests:
    1. Per-spec review requests — one per spec file (Phase 1 foundation)
    2. Verification requests — one per finding (Phase 3, web search fact-check)

Since verification depends on review results, batch mode requires two
sequential batches:
    - Batch 1: All per-spec review requests
    - Batch 2: All verification requests (built from Batch 1 results)

Usage:
    from batch import submit_review_batch, poll_batch, retrieve_review_results

    # Submit all specs as one batch
    job = submit_review_batch(specs, project_context="New elementary school")

    # Poll until complete
    while True:
        status = poll_batch(job.batch_id)
        if status["status"] == "ended":
            break
        time.sleep(10)

    # Retrieve and parse results
    results = retrieve_review_results(job)

Design decisions:
    - Each batch request uses the same system prompt and per-spec user message
      as the real-time review path (consistency)
    - custom_id format: "review__{sanitized_filename}__{index}" for reviews,
      "verify__{sanitized_filename}__{index}" for verifications
    - Partial failures are handled gracefully — errored/expired/canceled
      requests produce empty ReviewResults with error messages
    - The module is stateless — BatchJob dataclass holds all metadata needed
      to retrieve and parse results later
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
    """Tracks a batch submission and its metadata.

    Attributes:
        batch_id: Anthropic batch ID (e.g., "msgbatch_...")
        job_type: "review" or "verify"
        request_map: Maps custom_id -> metadata dict for result parsing
        created_at: Unix timestamp when the batch was submitted
        status: Current batch status (updated by poll_batch)
    """
    batch_id: str
    job_type: str               # "review" or "verify"
    request_map: dict           # custom_id -> metadata
    created_at: float
    status: str = "submitted"   # submitted, processing, ended, failed


@dataclass
class BatchStatus:
    """Snapshot of batch processing status.

    Attributes:
        status: Overall batch status ("in_progress", "ended", "canceling", etc.)
        processing: Number of requests still processing
        succeeded: Number of requests that completed successfully
        errored: Number of requests that errored
        canceled: Number of requests that were canceled
        expired: Number of requests that expired
        total: Total number of requests in the batch
    """
    status: str
    processing: int
    succeeded: int
    errored: int
    canceled: int
    expired: int
    total: int

    @property
    def completed(self) -> int:
        """Number of requests that are no longer processing."""
        return self.succeeded + self.errored + self.canceled + self.expired

    @property
    def progress_pct(self) -> float:
        """Completion percentage (0-100)."""
        return (self.completed / self.total * 100) if self.total > 0 else 0.0


def _sanitize_custom_id(filename: str, max_len: int = 50) -> str:
    """Sanitize a filename for use in a batch custom_id.

    Custom IDs must be alphanumeric with underscores and hyphens only.
    """
    stem = Path(filename).stem if "." in filename else filename
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", stem)
    return safe[:max_len]


def submit_review_batch(
    specs: list,
    *,
    project_context: str = "",
    model: str = MODEL_OPUS_46,
) -> BatchJob:
    """
    Submit all spec reviews as a single Anthropic Message Batch.

    Each spec gets its own batch request using the same system prompt and
    per-spec user message as the real-time review path.

    Args:
        specs: List of ExtractedSpec objects to review
        project_context: Optional project description (included in each request)
        model: Model ID to use (default: Claude Opus 4.6)

    Returns:
        BatchJob with batch_id and request mapping for result retrieval

    Raises:
        ValueError: If specs list is empty
        anthropic.APIError: If batch submission fails
    """
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
    """Check batch processing status.

    Args:
        batch_id: Anthropic batch ID to check

    Returns:
        BatchStatus with current counts and overall status
    """
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


def retrieve_review_results(job: BatchJob) -> dict[str, ReviewResult]:
    """
    Retrieve and parse review results from a completed batch.

    Iterates over all batch results, parses each response using the same
    JSON extraction logic as the real-time path, and returns a dict mapping
    filename to ReviewResult.

    Args:
        job: BatchJob returned by submit_review_batch()

    Returns:
        Dict mapping filename -> ReviewResult. Errored/expired/canceled
        requests produce ReviewResults with an error message and empty findings.
    """
    client = Anthropic(api_key=_get_api_key())
    results_by_file: dict[str, ReviewResult] = {}

    for result in client.messages.batches.results(job.batch_id):
        custom_id = result.custom_id
        meta = job.request_map.get(custom_id)
        if not meta:
            continue

        filename = meta["filename"]

        if result.result.type == "succeeded":
            message = result.result.message

            # Extract text from response content blocks
            response_text = ""
            for block in message.content:
                if hasattr(block, "text"):
                    response_text += block.text

            # Parse findings using the same logic as the streaming path
            try:
                data, thinking = _extract_json_array(response_text)
                findings = _parse_findings(data)
            except Exception:
                findings = []
                thinking = response_text

            # Extract token usage
            usage = message.usage if hasattr(message, "usage") else None
            input_tokens = getattr(usage, "input_tokens", 0) if usage else 0
            output_tokens = getattr(usage, "output_tokens", 0) if usage else 0

            results_by_file[filename] = ReviewResult(
                findings=findings,
                raw_response=response_text,
                thinking=thinking,
                model=MODEL_OPUS_46,
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
            )
        else:
            # errored, expired, or canceled
            error_msg = f"Batch request {result.result.type}"
            if hasattr(result.result, "error") and result.result.error:
                error_msg += f": {result.result.error}"

            results_by_file[filename] = ReviewResult(
                findings=[],
                error=error_msg,
            )

    return results_by_file


def cancel_batch(batch_id: str) -> str:
    """Cancel a running batch.

    Args:
        batch_id: Anthropic batch ID to cancel

    Returns:
        New processing status string after cancellation request
    """
    client = Anthropic(api_key=_get_api_key())
    batch = client.messages.batches.cancel(batch_id)
    return batch.processing_status