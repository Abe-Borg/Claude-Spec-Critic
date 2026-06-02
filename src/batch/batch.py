"""Batch processing for Spec Critic using Anthropic Message Batches API."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from ..review.reviewer import Finding, ReviewResult, _extract_json_array, _parse_findings, _get_client
from ..core.code_cycles import CodeCycle, DEFAULT_CYCLE
from ..review.review_request_builder import ReviewRequestSpec, build_review_request
from ..core.api_config import (
    BATCH_OUTPUT_BETA,
    PHASE_VERIFICATION,
    REVIEW_MODEL_DEFAULT,
    assert_extended_output_allowed,
    extract_cache_usage,
    output_cap_for_model,
)
from ..review.structured_schemas import (
    REVIEW_TOOL_NAME,
    extract_tool_use_block,
    structured_tool_output_enabled,
    verification_verdict_tool,
)
from ..tracing import capture_hooks as _trace

_log = logging.getLogger(__name__)


@dataclass
class BatchJob:
    batch_id: str
    job_type: str
    request_map: dict
    created_at: float
    status: str = "submitted"
    # Populated by start_batch_verification so collect_batch_verification_results
    # can pass the exact submitted list (not the full pre-pass input) to
    # collect_verification_batch_results, avoiding finding-index mismatch when
    # local-skip or cache-hit findings are filtered out before submission.
    submitted_findings: list | None = None


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


# The 300k extended-output beta header (``output-300k-2026-03-24``) is pinned
# in api_config. Betas get retired/renamed, and an *unrecognized* anthropic-beta
# value is rejected by the API with HTTP 400 — the exact failure mode the
# retired ``web-fetch-2026-02-09`` header caused on the common path (see
# CLAUDE.md "Web-fetch for follow-up reads"). The review batch is the only
# 300k-beta call site. Rather than let a retired header crash every large-input
# (>=200k-token) run at submit, the submit helper degrades gracefully: it clamps
# the extended requests back to the model's standard ceiling and re-submits on
# the non-beta path. Output may truncate on very large specs, which the existing
# review-stage failure surfacing already reports — strictly better than a crash.


def _is_beta_header_rejection(exc: Exception) -> bool:
    """True iff ``exc`` is the API rejecting the ``anthropic-beta`` header.

    The signature is an HTTP 400 whose message names the header, e.g.
    ``invalid_request_error: Unexpected value(s) "output-300k-2026-03-24" for
    the anthropic-beta header``. Match on the header name (the strong signal)
    and refuse to treat a non-400 as a header rejection, so unrelated errors
    are never swallowed by the fallback. When the status code can't be read
    (duck-typed/mocked exceptions), fall back to the message signature alone.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    if status is not None and status != 400:
        return False
    message = getattr(exc, "message", None)
    text = (message if isinstance(message, str) else str(exc)).lower()
    return "anthropic-beta" in text or ("beta" in text and "header" in text)


def _clamp_requests_to_model_ceiling(batch_requests: list[dict], *, model: str) -> None:
    """Clamp each request's ``max_tokens`` to ``model``'s standard ceiling.

    Used on the beta-rejection fallback: without the 300k beta a request asking
    for >128k output would itself be rejected, so the extended requests must
    drop to the non-beta ceiling before re-submission. Non-extended requests are
    already at/below the ceiling, so the clamp is a no-op for them.
    """
    for req in batch_requests:
        params = req.get("params") if isinstance(req, dict) else None
        if not isinstance(params, dict):
            continue
        requested = params.get("max_tokens")
        if isinstance(requested, int):
            params["max_tokens"] = output_cap_for_model(model, requested=requested)


def _create_review_batch(
    client, batch_requests: list[dict], *, use_beta: bool, model: str
) -> tuple[Any, bool]:
    """Submit the review batch, degrading gracefully on a beta-header rejection.

    Returns ``(message_batch, used_beta)``. When ``use_beta`` is True the 300k
    extended-output beta is attempted first; if the API rejects the header
    (retired/renamed), the requests are clamped to the model ceiling and
    re-submitted on the non-beta path so a stale header degrades output rather
    than crashing the whole run. Any error that is NOT a beta-header rejection
    propagates unchanged.
    """
    if use_beta:
        try:
            mb = client.beta.messages.batches.create(
                requests=batch_requests, betas=[BATCH_OUTPUT_BETA]
            )
            return mb, True
        except Exception as exc:
            if not _is_beta_header_rejection(exc):
                raise
            _log.warning(
                "Extended-output beta header %r was rejected by the API (%s). "
                "Falling back to the standard output ceiling for this review "
                "batch — large specs may have their review output truncated. "
                "Update BATCH_OUTPUT_BETA if the beta was renamed.",
                BATCH_OUTPUT_BETA,
                exc,
            )
            _clamp_requests_to_model_ceiling(batch_requests, model=model)
    mb = client.messages.batches.create(requests=batch_requests)
    return mb, False


def submit_review_batch(
    specs: list,
    *,
    project_context: str = "",
    model: str = REVIEW_MODEL_DEFAULT,
    cycle: CodeCycle = DEFAULT_CYCLE,
    retry_instruction: str | None = None,
    pre_detected_alerts: dict[str, list[dict]] | None = None,
) -> BatchJob:
    if not specs:
        raise ValueError("No specs to submit for batch review")
    client = _get_client()
    # Route every spec through the central review request builder
    # so the batch path, the real-time path, and the token preflight share
    # the same request-shape contributors (system prompt + cache control,
    # user message with paragraph map / pre_detected alerts, structured
    # tool, thinking, effort, max_tokens, service tier). A future change
    # to any of those lands in one place rather than three.
    batch_requests = []
    request_map = {}
    any_extended_output = False
    for idx, spec in enumerate(specs):
        custom_id = f"review__{_sanitize_custom_id(spec.filename)}__{idx}"
        spec_pre_detected = (
            pre_detected_alerts.get(spec.filename) if pre_detected_alerts else None
        )
        built = build_review_request(
            ReviewRequestSpec(
                spec_content=spec.content,
                filename=spec.filename,
                model=model,
                cycle=cycle,
                project_context=project_context,
                paragraph_map=spec.paragraph_map,
                pre_detected_alerts=spec_pre_detected,
                retry_instruction=retry_instruction,
            )
        )
        if built.allow_extended_output:
            any_extended_output = True
        # Fail-fast guard: 300k requires the batch beta header. Plan
        # Sprint 2 item 8 — never let a 300k request slip through without
        # it. The builder still computes ``max_tokens`` for the extended
        # path so the check fires before we hand the params to the SDK.
        betas = (
            [BATCH_OUTPUT_BETA]
            if (built.allow_extended_output and hasattr(client, "beta"))
            else None
        )
        assert_extended_output_allowed(
            max_tokens=built.params["max_tokens"], betas=betas, model=model
        )
        batch_requests.append({"custom_id": custom_id, "params": built.params})
        request_map[custom_id] = {"filename": spec.filename, "index": idx, "type": "review"}

    use_beta = any_extended_output and hasattr(client, "beta")
    # ``used_beta`` reflects whether the beta path actually succeeded — the
    # graceful fallback flips it to False after clamping, so the trace note
    # records what really happened rather than what was attempted.
    mb, used_beta = _create_review_batch(
        client, batch_requests, use_beta=use_beta, model=model
    )
    _trace.capture_note(
        None, "review batch submitted",
        batch_id=mb.id, request_count=len(batch_requests),
        extended_output_beta=used_beta,
    )
    return BatchJob(batch_id=mb.id, job_type="review", request_map=request_map, created_at=time.time())


def poll_batch(batch_id: str) -> BatchStatus:
    client = _get_client()
    batch = client.messages.batches.retrieve(batch_id)
    counts = batch.request_counts
    return BatchStatus(status=batch.processing_status, processing=counts.processing, succeeded=counts.succeeded, errored=counts.errored, canceled=counts.canceled, expired=counts.expired, total=(counts.processing + counts.succeeded + counts.errored + counts.canceled + counts.expired))


def _collect_batch_results_with_retry(batch_id: str, *, log=None) -> dict[str, Any]:
    """Stream a batch's results into a ``{custom_id: result}`` dict, retrying
    the whole stream on connection-class failures.

    ``client.messages.batches.results()`` opens a single long-lived chunked
    HTTPS download and parses it row-by-row. A mid-stream drop — the
    ``incomplete chunked read`` / ``peer closed connection`` family raised by
    httpx/httpcore when a proxy, firewall, or the server closes the connection
    before the final chunk — aborts the iteration and discards all partial
    progress. The SDK's own ``max_retries`` does not cover this: it retries
    acquiring the response, not a body that drops mid-stream.

    The results stream is not resumable, so each retry re-issues the request
    and rebuilds the dict from scratch (idempotent — results are keyed by
    ``custom_id``). Retryable classes (CONNECTION / SERVER_ERROR / RATE_LIMIT,
    via :func:`classify_exception`) back off per the shared realtime retry
    policy and retry; anything else propagates unchanged.

    A re-issued stream restarts from byte zero, so this recovers a transient
    blip but cannot beat a middlebox that severs *every* attempt at a fixed
    duration shorter than the full download — that needs a network/proxy
    timeout fix, surfaced via the warning log here.
    """
    from ..verification.retry_policy import (
        DEFAULT_REALTIME_RETRY_POLICY as _POLICY,
        classify_exception,
        compute_backoff_seconds,
        is_retryable_failure_class,
    )

    client = _get_client()
    attempts = max(1, _POLICY.max_attempts)
    for attempt in range(attempts):
        try:
            results: dict[str, Any] = {}
            for result in client.messages.batches.results(batch_id):
                results[result.custom_id] = result
            return results
        except Exception as exc:  # noqa: BLE001 — classified, re-raised if terminal
            failure_class = classify_exception(exc)
            if attempt + 1 >= attempts or not is_retryable_failure_class(failure_class):
                raise
            wait = compute_backoff_seconds(
                _POLICY, attempt=attempt, failure_class=failure_class
            )
            msg = (
                f"Batch results download interrupted ({failure_class.value}); "
                f"re-fetching in {wait:.0f}s (attempt {attempt + 2}/{attempts})"
            )
            if log is not None:
                log(msg, level="warning")
            else:
                logging.getLogger(__name__).warning(msg)
            time.sleep(wait)
    return {}  # unreachable: the loop returns on success or raises on the final attempt


def retrieve_review_results(job: BatchJob, *, model: str) -> dict[str, ReviewResult]:
    results: dict[str, ReviewResult] = {}
    for result in _collect_batch_results_with_retry(job.batch_id).values():
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

        # Tool-use stops are the success path when the model invoked the
        # ``submit_review_findings`` custom tool.
        if stop_reason not in ("end_turn", "tool_use"):
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
            structured_payload = extract_tool_use_block(message, REVIEW_TOOL_NAME)
            if isinstance(structured_payload, dict):
                data = structured_payload.get("findings") or []
                if not isinstance(data, list):
                    data = []
                thinking = str(structured_payload.get("analysis_summary") or "")
                payload_for_diag: dict | None = structured_payload
            else:
                data, thinking = _extract_json_array(response_text, stop_reason=stop_reason)
                payload_for_diag = None
            findings = _parse_findings(data)
            results[custom_id] = ReviewResult(
                findings=findings, raw_response=response_text, thinking=thinking,
                model=model, input_tokens=input_tokens, output_tokens=output_tokens,
                cache_creation_input_tokens=cache["cache_creation_input_tokens"],
                cache_read_input_tokens=cache["cache_read_input_tokens"],
                stop_reason=stop_reason, parse_status="ok",
                structured_payload=payload_for_diag,
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


# ``retrieve_verification_results`` (text-only legacy batch
# parser) was removed because (a) it had no callers, and (b) it pre-dates
# structured tool use and treated every non-``end_turn`` stop reason as
# incomplete. Under structured outputs the model frequently stops with
# ``tool_use`` after emitting ``submit_verification_verdict``; the legacy
# function would have misclassified those as failures. The canonical
# parser lives in ``verifier.parse_verification_response`` and is consumed
# by both the real-time path (``verifier._run_verification_call``) and the
# batch wave path (``verifier._classify_wave_results``). The
# detail-retrieval helper below remains; it returns the raw batch result
# envelopes so wave parsing in ``verifier`` owns the parse decisions.


def retrieve_verification_results_detailed(job: BatchJob) -> dict[str, Any]:
    raw = _collect_batch_results_with_retry(job.batch_id)
    return {cid: r for cid, r in raw.items() if cid in job.request_map}


def verification_request_includes_verdict_tool() -> bool:
    """Whether verification request paths will attach the verdict tool.

    Source of truth for both the request payload and the system prompt.
    Wherever the prompt mentions ``submit_verification_verdict``, the
    request must actually include it — and vice versa. Defaults to mirror
    ``structured_tool_output_enabled()``.
    """
    return structured_tool_output_enabled()


def build_verification_tools_for_profile(
    profile,
    severity: str | None = None,
) -> list[dict]:
    """Build the verification request tool list (web_search + verdict tool).

    The web_search ``max_uses`` is taken from
    :func:`src.verification_profiles.profile_max_uses(profile, severity)`
    so profile sets the ceiling and severity modulates within it. The
    verdict tool inclusion respects
    :func:`verification_request_includes_verdict_tool`, so structured
    outputs being disabled drops it from the list.

    ``profile`` can be a :class:`VerificationProfile`, its string value,
    or ``None`` (treated as the constructability default). The helper
    lives in :mod:`batch` to avoid a circular import — :mod:`verifier`
    already depends on :mod:`batch`, not the reverse.
    """
    from ..core.api_config import build_web_search_tool  # local import — keeps the
    # `api_config` import surface inside this module small
    from ..verification.verification_profiles import profile_max_uses as _profile_max_uses

    max_uses = _profile_max_uses(profile, severity)
    web_tool = build_web_search_tool(max_uses=max_uses)
    tools: list[dict] = [web_tool]
    if verification_request_includes_verdict_tool():
        tools.append(verification_verdict_tool())
    return tools


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
    # Route every finding through the same selector and
    # request builder as the real-time path. ``select_routing`` reads
    # severity / profile / mode / model / thinking / search budget /
    # tool inclusion in one place; ``build_verification_request``
    # consumes the decision to produce the exact same request shape
    # the streaming path uses. This removes the earlier drift
    # where the batch initial pass applied ``thinking`` unconditionally
    # and used the profile-only ``max_uses`` ceiling regardless of
    # mode (a GRIPES finding routed through batch got the full
    # STANDARD_REASONING bundle even though real-time would have
    # given it STRICT_STRUCTURED).
    from ..verification.verification_routing import (
        build_verification_request,
        merge_extra_headers,
        select_routing,
    )

    # Per-item extra_headers (web_fetch beta on STANDARD/DEEP modes)
    # collect here. Forwarded at the batch level via
    # ``batches.create(extra_headers=...)`` — embedding them inside the
    # per-request ``params`` body would trigger ``invalid_request_error:
    # Extra inputs are not permitted`` from the batch API.
    extra_headers_seq: list[dict[str, str]] = []
    for batch_idx, (finding_idx, finding) in enumerate(verifiable):
        custom_id = f"verify__{batch_idx}"
        decision = select_routing(
            finding,
            escalated=False,
            local_skip=False,
            model_override=model,
            cache_phase=PHASE_VERIFICATION,
        )
        verification_request = build_verification_request(
            decision,
            prompt=build_prompt_fn(finding),
            system_prompt=system_prompt_fn(cycle),
            assistant_content=None,
            include_service_tier=True,
        )
        extra_headers_seq.append(verification_request.extra_headers)
        reqs.append({"custom_id": custom_id, "params": verification_request.params})
        request_map[custom_id] = {
            "batch_idx": batch_idx,
            "finding_idx": finding_idx,
            "model": decision.model,
            "severity": decision.severity,
            "profile": decision.profile.value,
            # Stash the full routing decision so the wave
            # parser can stamp the result with the actual policy that
            # produced this request (rather than re-deriving the mode
            # from the finding alone).
            "routing": decision.to_dict(),
        }

    # Verification output is capped at 32k, well within both Sonnet and Opus
    # base ceilings, so the 300k extended-output beta is not needed. Use the
    # standard batches endpoint.
    union_headers = merge_extra_headers(extra_headers_seq)
    create_kwargs: dict[str, Any] = {"requests": reqs}
    if union_headers:
        create_kwargs["extra_headers"] = union_headers
    mb = client.messages.batches.create(**create_kwargs)
    _trace.capture_note(
        None, "verification batch submitted",
        batch_id=mb.id, request_count=len(reqs),
    )

    return BatchJob(batch_id=mb.id, job_type="verify", request_map=request_map, created_at=time.time())


def submit_verification_followup_wave(
    requests: list[dict[str, Any]],
    request_map: dict[str, Any],
    *,
    extra_headers: dict[str, str] | None = None,
) -> BatchJob:
    if not requests:
        raise ValueError("No verification follow-up requests to submit")
    client = _get_client()
    create_kwargs: dict[str, Any] = {"requests": requests}
    if extra_headers:
        create_kwargs["extra_headers"] = extra_headers
    mb = client.messages.batches.create(**create_kwargs)
    _trace.capture_note(
        None, "verification followup wave submitted",
        batch_id=mb.id, request_count=len(requests),
    )
    return BatchJob(batch_id=mb.id, job_type="verify", request_map=request_map, created_at=time.time())
