"""Central review request builder.

Single source of truth for review API request construction. Batch
review (:mod:`src.batch`) and the token preflight in
:func:`src.pipeline._prepare_specs` build their request kwargs through
this module so the shape they count is the shape they send.

Why this exists
---------------

Previously, review request shapes were constructed independently by
``batch.submit_review_batch`` (batch submission) and
``pipeline._prepare_specs`` (exact-count preflight).

The preflight in particular only counted ``system + project_context +
spec_content`` and did NOT include the ``<pre_detected>`` alert block
that batch later appended. A spec with a small body but a large alert
block could pass preflight and then exceed ``RECOMMENDED_MAX`` at
submission. The plan calls this out explicitly: "Token preflight cannot
miss the alert block, paragraph map, tool schema, or wrappers."

Centralizing the build also closes the smaller drift risks: cache-
control breakpoints, ``thinking`` / ``output_config.effort`` policy,
``service_tier``, the ``submit_review_findings`` tool, the
``tool_choice`` shape, and the ``output-300k-2026-03-24`` beta gate now
flow through one code path. A future API change touches one place.

Design
------

``ReviewRequestSpec`` is the (frozen) input record that fully describes
one review request. :func:`build_review_request` returns a
``BuiltReviewRequest`` carrying the final kwargs dict plus the raw
prompt / tools / phase so callers can introspect without re-running the
builder. :func:`build_token_count_request` returns the same dict
stripped to the fields the Anthropic ``count_tokens`` endpoint accepts.
:func:`review_request_cache_key` hashes the inputs that materially
affect the count so a cached exact count is only reused when those
inputs are unchanged (notably ``pre_detected_alerts``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, TYPE_CHECKING

from ..core.api_config import (
    LARGE_REVIEW_INPUT_THRESHOLD,
    PHASE_REVIEW,
    apply_effort_config,
    apply_thinking_config,
    batch_service_tier,
    model_supports_extended_output_beta,
    review_max_tokens,
    system_prompt_with_cache,
    tools_with_cache,
)
from ..core.code_cycles import CodeCycle, DEFAULT_CYCLE
from .prompts import get_single_spec_user_message, get_system_prompt
from .structured_schemas import (
    review_findings_tool,
    review_tool_choice,
    structured_tool_output_enabled,
)
from ..core.tokenizer import count_tokens

if TYPE_CHECKING:
    from ..input.extractor import ParagraphMapping


@dataclass(frozen=True)
class ReviewRequestSpec:
    """Inputs that fully describe one review request.

    Every field that materially affects the request shape (and therefore
    its input-token count) lives here. The builder reads exclusively
    from this record so the path that counts a request and the path that
    sends a request cannot fall out of sync.

    Review runs exclusively through the Message Batches API, so the
    builder always produces a batch-shaped request (batch cache phase,
    service tier, extended-output gating). ``force_allow_extended_output``
    is an escape hatch for tests; production callers leave it ``None``
    and let the builder decide.
    """

    spec_content: str
    filename: str
    model: str
    cycle: CodeCycle = DEFAULT_CYCLE
    project_context: str = ""
    paragraph_map: "Optional[Sequence[ParagraphMapping]]" = None
    pre_detected_alerts: "Optional[Sequence[Mapping[str, object]]]" = None
    retry_instruction: Optional[str] = None
    force_allow_extended_output: Optional[bool] = None
    include_service_tier: Optional[bool] = None


@dataclass
class BuiltReviewRequest:
    """Final request payload + the raw inputs used to materialize it.

    ``params`` is the kwargs dict appended into
    ``messages.batches.create(requests=[...])`` (batch). The raw prompt
    / user message / tools list are surfaced so preflight, diagnostics,
    and tests can recover the same shape without re-running the builder.
    """

    params: dict[str, Any]
    system_prompt: str
    user_message: str
    tools: Optional[list[dict]]
    phase: str
    model: str
    allow_extended_output: bool


def build_user_message(spec: ReviewRequestSpec) -> str:
    """Materialize the per-spec user message exactly as it will be sent.

    Includes the ``<pre_detected>`` alert block when
    ``pre_detected_alerts`` is supplied and the env toggle is on, the
    id-tagged paragraph rendering when ``paragraph_map`` is
    supplied and ids are enabled, and the optional repair-batch
    instruction suffix (the review repair path).
    """
    user_message = get_single_spec_user_message(
        spec.spec_content,
        spec.filename,
        project_context=spec.project_context,
        cycle=spec.cycle,
        paragraph_map=spec.paragraph_map,
        pre_detected_alerts=spec.pre_detected_alerts,
    )
    if spec.retry_instruction:
        user_message += f"\n\n{spec.retry_instruction}"
    return user_message


def _resolve_extended_output(
    spec: ReviewRequestSpec,
    *,
    system_prompt: str,
    user_message: str,
) -> bool:
    """Decide whether the 300k batch-output beta applies to this request.

    The decision combines model capability with the local cl100k_base
    count of the actual request shape — small batches stay on the 128k
    cap, large batches lift to 300k. Reading the capability from the
    central registry lets Sonnet 4.6 use the path correctly.
    """
    if spec.force_allow_extended_output is not None:
        return bool(spec.force_allow_extended_output)
    if not model_supports_extended_output_beta(spec.model):
        return False
    approx_input_tokens = count_tokens(system_prompt) + count_tokens(user_message)
    return approx_input_tokens >= LARGE_REVIEW_INPUT_THRESHOLD


def _build_params_from_strings(
    *,
    system_prompt: str,
    user_message: str,
    model: str,
    allow_extended_output: bool,
    include_service_tier: bool,
) -> tuple[dict[str, Any], Optional[list[dict]]]:
    """Build review request kwargs from already-materialized prompts.

    Inner helper used by :func:`build_review_request`. Centralizing the
    request-shape construction here keeps the path that counts a request
    and the path that sends it from drifting.
    """
    system_payload = system_prompt_with_cache(system_prompt, phase=PHASE_REVIEW)

    use_tool = structured_tool_output_enabled()
    if use_tool:
        tools = tools_with_cache([review_findings_tool(model=model)], phase=PHASE_REVIEW)
    else:
        tools = None

    output_limit = review_max_tokens(
        model=model,
        allow_extended_output=allow_extended_output,
    )

    params: dict[str, Any] = {
        "model": model,
        "max_tokens": output_limit,
        "system": system_payload,
        "messages": [{"role": "user", "content": user_message}],
    }
    apply_thinking_config(params, model=model, phase=PHASE_REVIEW)
    apply_effort_config(params, model=model, phase=PHASE_REVIEW)
    if use_tool:
        params["tools"] = tools
        params["tool_choice"] = review_tool_choice()

    if include_service_tier:
        tier = batch_service_tier()
        if tier:
            params["service_tier"] = tier

    return params, tools


def build_review_request(spec: ReviewRequestSpec) -> BuiltReviewRequest:
    """Build the kwargs dict for a single review request.

    The returned ``params`` is what gets handed to the SDK; the
    surrounding fields on :class:`BuiltReviewRequest` are kept so
    callers can introspect the shape without re-running the builder.

    Invariant: every call site that submits a review request
    routes through this function. If a future change adds a new
    request-shape contributor (a new tool, a new beta header, a new
    sampling param), this is the one place it lands so token preflight
    and submission cannot drift.
    """
    system_prompt = get_system_prompt(spec.cycle)
    user_message = build_user_message(spec)
    allow_extended = _resolve_extended_output(
        spec, system_prompt=system_prompt, user_message=user_message
    )
    include_tier = (
        spec.include_service_tier
        if spec.include_service_tier is not None
        else True
    )
    params, tools = _build_params_from_strings(
        system_prompt=system_prompt,
        user_message=user_message,
        model=spec.model,
        allow_extended_output=allow_extended,
        include_service_tier=include_tier,
    )
    return BuiltReviewRequest(
        params=params,
        system_prompt=system_prompt,
        user_message=user_message,
        tools=tools,
        phase=PHASE_REVIEW,
        model=spec.model,
        allow_extended_output=allow_extended,
    )


def build_token_count_request(
    spec: ReviewRequestSpec,
) -> tuple[BuiltReviewRequest, dict[str, Any]]:
    """Build a request shape suitable for ``count_tokens_via_api``.

    Returns ``(built, count_kwargs)`` where ``count_kwargs`` can be
    splatted into :func:`src.tokenizer.count_tokens_via_api`. The
    returned shape matches the actual production request shape — same
    system prompt, same user message (with pre-detected alerts and the
    paragraph map), same tool definition — so the count cannot
    underestimate.

    The cache-control wrappers on ``system`` and ``tools`` are stripped
    because they are pricing hints, not part of the input token count.
    Sending them through ``count_tokens`` either no-ops (raw text
    returned) or is rejected depending on SDK version; the raw form is
    portable and gives the same count.
    """
    built = build_review_request(spec)
    count_kwargs: dict[str, Any] = {
        "model": built.model,
        "system": built.system_prompt,
        "messages": built.params["messages"],
    }
    if built.tools is not None:
        # Recompute the raw tool list without the cache_control block.
        count_kwargs["tools"] = [review_findings_tool(model=built.model)]
    return built, count_kwargs


def review_request_cache_key(spec: ReviewRequestSpec) -> str:
    """SHA-256 of the inputs that materially affect the input-token count.

    Routes through :func:`src.extraction_cache.token_count_cache_key`
    so the on-disk cache layout stays compatible. Includes
    ``pre_detected_alerts`` (via the rendered user message), the
    paragraph map (same), the tool schema, and the cycle label — every
    input that can move the count.
    """
    from ..input.extraction_cache import token_count_cache_key

    system_prompt = get_system_prompt(spec.cycle)
    user_message = build_user_message(spec)
    tools = [review_findings_tool(model=spec.model)] if structured_tool_output_enabled() else None
    return token_count_cache_key(
        model=spec.model,
        system_prompt=system_prompt,
        user_message=user_message,
        project_context=spec.project_context,
        cycle_label=spec.cycle.label,
        tools=tools,
    )


def estimate_local_request_tokens(spec: ReviewRequestSpec) -> int:
    """Local cl100k_base count of ``system + user_message`` for this request.

    Used by preflight to rank specs when an exact-count budget cannot
    afford every spec. Counts the *full* user message — including the
    ``<pre_detected>`` block and id-tagged paragraphs — so a spec with
    a small body but a large alert block is not incorrectly ranked
    below a larger raw spec. This is the rank we use to pick exact-
    count candidates (plan task 7: "Reordering files does not cause a
    smaller raw spec to bypass exact-count checks when its wrapper /
    alerts make it larger").
    """
    system_prompt = get_system_prompt(spec.cycle)
    user_message = build_user_message(spec)
    return count_tokens(system_prompt) + count_tokens(user_message)
