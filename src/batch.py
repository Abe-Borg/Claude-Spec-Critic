"""Batch processing for Spec Critic using Anthropic Message Batches API."""

from __future__ import annotations

import re
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from .reviewer import Finding, ReviewResult, _extract_json_array, _parse_findings, _get_client, MODEL_OPUS_47
from .code_cycles import CodeCycle, DEFAULT_CYCLE
from .review_request_builder import ReviewRequestSpec, build_review_request
from .api_config import (
    BATCH_OUTPUT_BETA,
    PHASE_VERIFICATION,
    REVIEW_MODEL_DEFAULT,
    VERIFICATION_MODEL_DEFAULT as VERIFICATION_MODEL,
    WEB_SEARCH_TOOL,
    apply_effort_config,
    apply_thinking_config,
    assert_extended_output_allowed,
    batch_service_tier,
    extract_cache_usage,
    system_prompt_with_cache,
    tools_with_cache,
    verification_max_tokens,
    web_search_tool_for_severity,
)
from .structured_schemas import (
    REVIEW_TOOL_NAME,
    VERIFICATION_TOOL_NAME,
    extract_tool_use_block,
    structured_tool_output_enabled,
    verification_verdict_tool,
)


@dataclass
class BatchJob:
    batch_id: str
    job_type: str
    request_map: dict
    created_at: float
    status: str = "submitted"
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
                batch=True,
            )
        )
        if built.allow_extended_output:
            any_extended_output = True
        betas = (
            [BATCH_OUTPUT_BETA]
            if (built.allow_extended_output and hasattr(client, "beta"))
            else None
        )
        assert_extended_output_allowed(
            max_tokens=built.params["max_tokens"], betas=betas
        )
        batch_requests.append({"custom_id": custom_id, "params": built.params})
        request_map[custom_id] = {"filename": spec.filename, "index": idx, "type": "review"}

    use_beta = any_extended_output and hasattr(client, "beta")
    create_fn = client.beta.messages.batches.create if use_beta else client.messages.batches.create
    kwargs: dict[str, Any] = {"requests": batch_requests}
    if use_beta:
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
    if hasattr(error_obj, "error"):
        inner = error_obj.error
        if hasattr(inner, "message"):
            msg = str(inner.message)
            error_type = str(getattr(inner, "type", "")) or ""
            if error_type:
                return f"{error_type}: {msg}"
            return msg
    if hasattr(error_obj, "message"):
        return str(error_obj.message)
    s = str(error_obj)
    return s[:200] if len(s) > 200 else s




def retrieve_verification_results_detailed(job: BatchJob) -> dict[str, Any]:
    client = _get_client()
    results: dict[str, Any] = {}
    for result in client.messages.batches.results(job.batch_id):
        if result.custom_id not in job.request_map:
            continue
        results[result.custom_id] = result
    return results


def verification_request_includes_verdict_tool() -> bool:
    """Whether verification request paths will attach the verdict tool.

    Source of truth for both the request payload and the system prompt.
    Wherever the prompt mentions ``submit_verification_verdict``, the
    request must actually include it — and vice versa. Defaults to mirror
    ``structured_tool_output_enabled()``.
    """
    return structured_tool_output_enabled()


def build_verification_tools(severity: str | None = None) -> list[dict]:
    """Build the verification request tool list (Chunk C).

    Single source of truth for verification tool payloads. Returns the
    web_search tool with the severity-tiered ``max_uses`` budget plus the
    custom ``submit_verification_verdict`` tool when the tool-output flag
    is on. Cache controls are NOT applied here — wrap with
    :func:`tools_with_cache` at the call site if a cache breakpoint should
    pin the tools prefix.

    Every verification path (real-time initial, batch initial, batch retry,
    batch continuation) must build its tools through this helper. The
    Chunk C invariant is that the prompt and the tools list never disagree
    about which tools the model has access to.
    """
    web_tool = web_search_tool_for_severity(severity) if severity is not None else WEB_SEARCH_TOOL
    tools: list[dict] = [web_tool]
    if verification_request_includes_verdict_tool():
        tools.append(verification_verdict_tool())
    return tools


def build_verification_tools_for_profile(
    profile,
    severity: str | None = None,
) -> list[dict]:
    """Profile-aware variant of :func:`build_verification_tools` (Chunk H).

    The web_search ``max_uses`` is taken from
    :func:`src.verification_profiles.profile_max_uses(profile, severity)`
    so profile sets the ceiling and severity modulates within it. The
    verdict tool inclusion still respects
    :func:`verification_request_includes_verdict_tool`, identical to the
    severity-only helper, so structured outputs being disabled has the
    same effect on both paths.

    ``profile`` can be a :class:`VerificationProfile`, its string value,
    or ``None`` (treated as the constructability default). The helper
    lives in :mod:`batch` rather than :mod:`verifier` to mirror the
    existing helper and avoid a circular import — :mod:`verifier`
    already depends on :mod:`batch`, not the reverse.
    """
    from .api_config import build_web_search_tool
    from .verification_profiles import profile_max_uses as _profile_max_uses

    max_uses = _profile_max_uses(profile, severity)
    web_tool = build_web_search_tool(max_uses=max_uses)
    tools: list[dict] = [web_tool]
    if verification_request_includes_verdict_tool():
        tools.append(verification_verdict_tool())
    return tools


def _build_verification_request_params(
    *,
    prompt: str,
    system_prompt: str,
    assistant_content: list | None = None,
    model: str | None = None,
    severity: str | None = None,
    profile: Any = None,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    if assistant_content is not None:
        messages.append({"role": "assistant", "content": assistant_content})
    selected_model = model or VERIFICATION_MODEL
    if profile is not None:
        tool_list = build_verification_tools_for_profile(profile, severity)
    else:
        tool_list = build_verification_tools(severity)
    params: dict[str, Any] = {
        "model": selected_model,
        "max_tokens": verification_max_tokens(model=selected_model),
        "system": system_prompt_with_cache(system_prompt, phase=PHASE_VERIFICATION),
        "tools": tools_with_cache(tool_list, phase=PHASE_VERIFICATION),
        "messages": messages,
    }
    apply_thinking_config(params, model=selected_model, phase=PHASE_VERIFICATION)
    apply_effort_config(params, model=selected_model, phase=PHASE_VERIFICATION)
    tier = batch_service_tier()
    if tier:
        params["service_tier"] = tier
    return params


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
    from .verification_routing import (
        build_verification_request,
        select_routing,
    )

    for batch_idx, (finding_idx, finding) in enumerate(verifiable):
        custom_id = f"verify__{batch_idx}"
        decision = select_routing(
            finding,
            escalated=False,
            local_skip=False,
            model_override=model,
            cache_phase=PHASE_VERIFICATION,
        )
        params = build_verification_request(
            decision,
            prompt=build_prompt_fn(finding),
            system_prompt=system_prompt_fn(cycle),
            assistant_content=None,
            include_service_tier=True,
        )
        reqs.append({"custom_id": custom_id, "params": params})
        request_map[custom_id] = {
            "batch_idx": batch_idx,
            "finding_idx": finding_idx,
            "model": decision.model,
            "severity": decision.severity,
            "profile": decision.profile.value,
            "routing": decision.to_dict(),
        }

    mb = client.messages.batches.create(requests=reqs)

    return BatchJob(batch_id=mb.id, job_type="verify", request_map=request_map, created_at=time.time())


def submit_verification_followup_wave(
    requests: list[dict[str, Any]],
    request_map: dict[str, Any],
) -> BatchJob:
    if not requests:
        raise ValueError("No verification follow-up requests to submit")
    client = _get_client()
    mb = client.messages.batches.create(requests=requests)
    return BatchJob(batch_id=mb.id, job_type="verify", request_map=request_map, created_at=time.time())


def cancel_batch(batch_id: str) -> str:
    client = _get_client()
    batch = client.messages.batches.cancel(batch_id)
    return batch.processing_status
