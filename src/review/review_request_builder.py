"""Central review request builder.

Single source of truth for review API request construction. Batch
review (:mod:`src.batch`) and the token preflight in
:func:`src.pipeline._prepare_specs` build their request kwargs through
this module so the shape they count is the shape they send.

Why this exists
---------------

Previously, review request shapes were constructed independently by
``batch.submit_review_batch`` (batch submission) and
``pipeline._prepare_specs`` (the count-API preflight).

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
builder. :func:`build_token_count_request` returns the counting form of
that same dict — the fields Anthropic's ``count_tokens`` endpoint counts
(``core.request_budget.count_request_from_params``).

Sizing (plan WP-08)
-------------------
One rule sizes a review everywhere: :func:`review_input_count` counts the
request's input shape (it does not depend on ``max_tokens``) —
Anthropic's count estimate when one is available, else the padded local
count of every part, tool overhead included. The extended-output decision
(:func:`_allow_extended_output`: a beta-capable model and a count at or
above ``LARGE_REVIEW_INPUT_THRESHOLD``) reads that count, and only then is
the output cap chosen and the fit rechecked against it
(:func:`review_request_budget`). The token preflight asks the count API and
caches each estimate; the builder never calls the network, but it reads a
cached estimate for the identical shape, so a batch request is capped from
the same basis the preflight judged it on. The real-time gate reads the same
count (:func:`review_extended_output_count`).
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
from ..core.request_budget import (
    InputCount,
    RequestBudget,
    budget_for_count,
    count_request_from_params,
    resolve_input_count,
)
from .prompts import get_single_spec_user_message, get_system_prompt
from .structured_schemas import (
    review_findings_tool,
    review_tool_choice,
    structured_tool_output_enabled,
)
from ..core.tokenizer import RECOMMENDED_MAX, count_tokens

if TYPE_CHECKING:
    from ..input.extractor import ParagraphMapping


# Instruction suffix appended to a review request that retries a previously
# truncated / unparseable response. Shared by the batch repair pass
# (``pipeline._recover_retryable_review_batch_results``) and the real-time
# runner's inline repair attempt so the two transports issue the identical
# retry prompt. Cache-safe: ``build_user_message`` appends it after the spec
# body, past every prompt-cache breakpoint.
RETRY_TRUNCATED_REVIEW_INSTRUCTION = (
    "This is a retry of a previously truncated review. Submit findings via the "
    "submit_review_findings tool with analysis_summary set to an empty string. "
    "Spend the entire output budget on the findings array."
)


@dataclass(frozen=True)
class ReviewRequestSpec:
    """Inputs that fully describe one review request.

    Every field that materially affects the request shape (and therefore
    its input-token count) lives here. The builder reads exclusively
    from this record so the path that counts a request and the path that
    sends a request cannot fall out of sync.

    The same built request serves both review transports: the Message
    Batches path wraps ``params`` in a ``{custom_id, params}`` envelope,
    and the real-time path streams it via ``client.messages.stream``
    (with ``include_service_tier=False`` and extended output pinned off —
    the 300k output beta is batch-only by API design).
    ``force_allow_extended_output`` is an escape hatch for tests;
    production callers leave it ``None`` and let the builder decide.
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
    # The count the extended-output decision read (``None`` when no count was
    # needed: the decision was forced, or the model has no extended path).
    input_count: Optional[InputCount] = None


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


def _allow_extended_output(spec: ReviewRequestSpec, count: Optional[InputCount]) -> bool:
    """Whether the 300k batch-output beta applies to this request.

    Forced by ``force_allow_extended_output`` when set (the real-time
    transport pins it off). Otherwise it needs a model the beta whitelists
    and an input count — the API estimate or the padded local estimate, the
    same basis the preflight judged the request on — at or above
    ``LARGE_REVIEW_INPUT_THRESHOLD``. Never the raw local count (plan WP-08):
    that runs low for the newer tokenizer and would leave a large spec on the
    128k cap. A request that could not be sized stays on the baseline cap.
    """
    if spec.force_allow_extended_output is not None:
        return bool(spec.force_allow_extended_output)
    if not model_supports_extended_output_beta(spec.model):
        return False
    return (
        count is not None
        and count.tokens is not None
        and count.tokens >= LARGE_REVIEW_INPUT_THRESHOLD
    )


def _needs_count(spec: ReviewRequestSpec) -> bool:
    return spec.force_allow_extended_output is None and model_supports_extended_output_beta(
        spec.model
    )


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
    include_tier = (
        spec.include_service_tier
        if spec.include_service_tier is not None
        else True
    )
    # The input shape does not depend on ``max_tokens``: build it on the
    # baseline cap, size it, and only then choose the cap (plan WP-08).
    params, tools = _build_params_from_strings(
        system_prompt=system_prompt,
        user_message=user_message,
        model=spec.model,
        allow_extended_output=False,
        include_service_tier=include_tier,
    )
    count = (
        resolve_input_count(
            count_request_from_params(params), use_api=False, local_counter=count_tokens
        )
        if _needs_count(spec)
        else None
    )
    allow_extended = _allow_extended_output(spec, count)
    if allow_extended:
        params["max_tokens"] = review_max_tokens(
            model=spec.model, allow_extended_output=True
        )
    return BuiltReviewRequest(
        params=params,
        system_prompt=system_prompt,
        user_message=user_message,
        tools=tools,
        phase=PHASE_REVIEW,
        model=spec.model,
        allow_extended_output=allow_extended,
        input_count=count,
    )


def build_token_count_request(
    spec: ReviewRequestSpec,
) -> tuple[BuiltReviewRequest, dict[str, Any]]:
    """Build a request and its ``count_tokens`` form.

    Returns ``(built, count_kwargs)`` where ``count_kwargs`` can be splatted
    into :func:`src.core.tokenizer.count_input_tokens` (or
    ``count_tokens_via_api``). It is derived from ``built.params`` by
    :func:`~src.core.request_budget.count_request_from_params` — the same
    system prompt, user message (with pre-detected alerts and the paragraph
    map), tool definition, ``tool_choice``, and ``thinking`` config the
    request sends, minus ``cache_control`` markers (pricing hints the count
    ignores) and the output settings that do not change the input size.
    """
    built = build_review_request(spec)
    return built, count_request_from_params(built.params)


def review_input_count(
    spec: ReviewRequestSpec,
    *,
    use_api: bool = False,
    client_factory=None,
    include_local: bool = False,
) -> InputCount:
    """The input size of ``spec``'s request, independent of its output cap.

    ``use_api=True`` asks Anthropic's count endpoint (and caches the
    estimate); ``use_api=False`` reads a cached estimate for this exact
    shape or falls back to the padded local count of every part of the
    request, tool overhead included. The local counter is this module's
    ``count_tokens``, read at call time.
    """
    system_prompt = get_system_prompt(spec.cycle)
    params, _tools = _build_params_from_strings(
        system_prompt=system_prompt,
        user_message=build_user_message(spec),
        model=spec.model,
        allow_extended_output=False,
        include_service_tier=False,
    )
    return resolve_input_count(
        count_request_from_params(params),
        use_api=use_api,
        client_factory=client_factory,
        local_counter=count_tokens,
        include_local=include_local,
    )


def review_request_budget(
    spec: ReviewRequestSpec,
    *,
    use_api: bool = False,
    client_factory=None,
    include_local: bool = False,
    count: Optional[InputCount] = None,
) -> RequestBudget:
    """Size ``spec``'s review request, choose its output cap, recheck the fit.

    The count comes first (:func:`review_input_count`, or ``count`` when the
    caller already has it), the extended-output decision reads it
    (:func:`_allow_extended_output`), and the fit is judged against the
    resulting cap: ``min(RECOMMENDED_MAX, context window - cap - reserve)``.
    :func:`build_review_request` applies the same decision to the same count
    basis, so the request that is sent is the one judged here.
    """
    if count is None:
        count = review_input_count(
            spec,
            use_api=use_api,
            client_factory=client_factory,
            include_local=include_local,
        )
    allow_extended = _allow_extended_output(spec, count)
    return budget_for_count(
        count,
        model=spec.model,
        output_reserve=review_max_tokens(
            model=spec.model, allow_extended_output=allow_extended
        ),
        phase_limit=RECOMMENDED_MAX,
    )


def review_extended_output_count(spec: ReviewRequestSpec) -> Optional[int]:
    """The count the extended-output threshold compares for ``spec``.

    The cached API estimate for this exact shape when the preflight made one,
    else the padded local estimate (``None`` when neither exists). The
    real-time transport's oversize gate reads it, so the gate and the batch
    builder's cap decision share one basis. Never calls the network.
    """
    return review_input_count(spec, use_api=False).tokens
