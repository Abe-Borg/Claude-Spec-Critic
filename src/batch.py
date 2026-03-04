"""
Batch processing for Spec Critic using the Anthropic Message Batches API.

Handles submission, polling, and result retrieval for batched spec reviews
and finding verifications. Provides 50% cost savings over real-time API calls
at the cost of asynchronous processing (typically under 1 hour).

The batch pipeline supports two types of requests:
    1. Per-spec review requests — one per spec file
    2. Verification requests — one per finding (web search fact-check)

Since verification depends on review results, batch mode requires two
sequential batches:
    - Batch 1: All per-spec review requests
    - Batch 2: All verification requests (built from Batch 1 results)

v1.7.0 — Verification batching + model selection.
    Added submit_verification_batch() and retrieve_verification_results()
    for routing verification through the Batches API at 50% cost savings.
    Added model parameter to submit_review_batch() so the user can choose
    between Opus and Sonnet for the review stage.

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
      "verify__{index}" for verifications
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
    MODEL_SONNET_46,
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


# ---------------------------------------------------------------------------
# Review batch
# ---------------------------------------------------------------------------

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
        model: Model ID to use (default: Claude Opus 4.6, also supports Sonnet 4.6)

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


def retrieve_review_results(job: BatchJob, model: str = MODEL_OPUS_46) -> dict[str, ReviewResult]:
    """
    Retrieve and parse review results from a completed batch.

    Iterates over all batch results, parses each response using the same
    JSON extraction logic as the real-time path, and returns a dict mapping
    filename to ReviewResult.

    Args:
        job: BatchJob returned by submit_review_batch()
        model: Model ID used for the review (for ReviewResult metadata)

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
                model=model,
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


# ---------------------------------------------------------------------------
# Verification batch (v1.7.0)
# ---------------------------------------------------------------------------

def submit_verification_batch(
    findings: list[Finding],
    build_prompt_fn,
) -> BatchJob:
    """Submit verification requests as a single Anthropic Message Batch.

    Each finding gets its own batch request using Sonnet 4.6 with the
    web_search tool. 

    Args:
        findings: List of Finding objects to verify. 
        build_prompt_fn: Function that takes a Finding and returns the
            verification prompt string. Injected from verifier.py to
            avoid circular imports.

    Returns:
        BatchJob with batch_id and request mapping for result retrieval.
        The request_map maps custom_id -> {"index": int, "finding_index": int}
        where finding_index is the position in the original findings list.

    Raises:
        ValueError: If no verifiable findings
        anthropic.APIError: If batch submission fails
    """
    # Build list of (original_index, finding) for verifiable findings
    verifiable: list[tuple[int, Finding]] = []
    for i, f in enumerate(findings):
        verifiable.append((i, f))

    if not verifiable:
        raise ValueError("No findings eligible for verification")

    # Sort by confidence ascending (least confident first) for custom_id ordering
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
                "model": MODEL_SONNET_46,
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
    """Retrieve verification results from a completed batch and populate findings.

    Iterates over batch results, parses each response using the verifier's
    parsing function, and sets finding.verification for each finding.

    Args:
        job: BatchJob returned by submit_verification_batch()
        findings: The original findings list (modified in-place)
        parse_response_fn: Function that takes response text and returns a
            VerificationResult. Injected from verifier.py to avoid circular imports.

    Returns:
        The same findings list (modified in-place) for convenience.
    """
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

            # Extract text from response content blocks
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

    # Set UNVERIFIED for any findings that weren't in the batch results
    for f in findings:
        if f.verification is None:
            f.verification = VerificationResult(
                verdict="UNVERIFIED",
                explanation="No verification result returned from batch.",
            )

    return findings


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