"""Chunk 4 — unified verification routing and request construction.

Single source of truth for verification request construction. Real-time
verification (:mod:`src.verifier._run_verification_call`), batch verification
(:mod:`src.batch.submit_verification_batch`), and the batch retry /
continuation builders (:func:`src.verifier._build_retry_request`,
:func:`src.verifier._build_continuation_request`) all build their request
kwargs through this module so that:

* The same finding produces the same routing decision regardless of which
  caller asks (real-time vs batch initial vs batch retry / continuation).
* The decision encodes the *full* policy bundle (model, thinking, effort,
  search budget, cache phase, tool inclusion, max continuations, escalation
  eligibility) in one record.
* The result-parsing path can stamp the *actual* routed mode / profile on
  the verification result by looking up the stored decision, rather than
  re-deriving it from the finding (which can disagree with what the request
  was actually built with).

Why this exists
---------------

Before Chunk 4, the verification routing decision lived in three places:

* ``verifier._run_verification_call`` — real-time path. Calls
  ``select_verification_mode`` + ``mode_policy``, applies the mode's
  ``thinking_enabled`` gate (skipping ``thinking`` for STRICT_STRUCTURED),
  scales ``max_uses`` by the mode multiplier, and stamps the routed mode
  + profile on the result.
* ``batch._build_verification_request_params`` — batch initial path. Calls
  ``apply_thinking_config`` unconditionally and uses the profile-aware
  ``max_uses`` ceiling *without* the mode multiplier. A GRIPES-severity
  finding that would have been STRICT_STRUCTURED in real-time (Sonnet, no
  thinking, half budget) ran through the batch path as STANDARD_REASONING
  (Sonnet, thinking on, full budget). The result was then *re-stamped* by
  the wave parser as STRICT_STRUCTURED — disagreeing with what the request
  actually sent.
* ``verifier._build_retry_request`` / ``_build_continuation_request`` —
  retry and continuation paths. Same problem as the batch initial path:
  they take ``model`` / ``severity`` / ``profile`` but not the mode, so
  thinking is applied unconditionally and the search budget is profile-
  only.

The plan calls this out directly: "Make batch verification and real-time
verification use the same routing decision and request builder."

Design
------

:class:`VerificationRoutingDecision` is the frozen input record. Every
routing decision an external caller might want to inspect (or persist for
diagnostics) lives there. :func:`select_routing` is the pure-function
selector — given a ``Finding`` plus the escalation / cached-mode hint, it
returns a fully-populated decision.

:func:`build_verification_request` is the single request builder. It
consumes a decision and the rendered prompt strings and produces the
kwargs dict the SDK accepts. The same function backs the real-time
streaming path, the batch initial submission, and the wave retry /
continuation submissions; ``assistant_content`` is the only branch (it is
present only for ``pause_turn`` continuation requests).

:func:`apply_routing_to_result` stamps the routed mode / profile /
escalation flag onto a :class:`VerificationResult` so the wave parser
(which reconstructs the decision from the stored ``request_contexts``
entry) and the real-time path apply identical telemetry.

Serializability
---------------

:meth:`VerificationRoutingDecision.to_dict` returns a JSON-safe dict so
the decision can be stashed in ``request_map`` / ``request_contexts`` and
reconstructed by :meth:`VerificationRoutingDecision.from_dict` in the wave
parser. Diagnostics may also dump the decision verbatim into per-finding
event payloads — the dict form is intentionally flat (strings, ints,
bools) so a future diagnostics aggregator can bucket by any field
without parsing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .api_config import (
    PHASE_VERIFICATION,
    PHASE_VERIFICATION_CONTINUATION,
    PHASE_VERIFICATION_RETRY,
    apply_effort_config,
    apply_thinking_config,
    batch_service_tier,
    model_supports_adaptive_thinking,
    system_prompt_with_cache,
    tools_with_cache,
    verification_max_tokens,
)
from .retry_policy import (
    DEFAULT_MAX_CONTINUATIONS,
    max_continuations_for_mode,
)
from .reviewer import Finding
from .verification_modes import (
    ModePolicy,
    VerificationMode,
    mode_policy,
    mode_search_budget,
    select_verification_mode,
)
from .verification_profiles import (
    VerificationProfile,
    classify_finding_profile,
    profile_max_uses,
)
from .verification_router import (
    classify_finding_for_verification,
    initial_verification_model,
    local_skip_enabled,
)


# Chunk 6: default max continuation count for the real-time path drops
# from the legacy 5 to 2. The deep-reasoning override (4) lives in
# :mod:`retry_policy.max_continuations_for_mode` and is applied by
# :func:`select_routing` after the mode is selected, so a single map
# governs the per-mode cap and a future tuning pass touches one
# constant. The batch wave loop bounds continuations through
# ``MAX_VERIFICATION_WAVES`` instead, so this field is only consulted
# on the real-time path.
_DEFAULT_MAX_CONTINUATIONS = DEFAULT_MAX_CONTINUATIONS

# Routing trace tags. Kept short so a diagnostics dump can bucket by tag
# without parsing free text. Stable on purpose — adding a new tag is fine
# but renaming an existing one breaks the aggregation.
TRACE_LOCAL_SKIP = "local_skip"
TRACE_LOCAL_SKIP_BYPASSED = "local_skip_bypassed_by_caller"
TRACE_CACHED_MODE = "cached_mode_replay"
TRACE_ESCALATED = "escalated_to_deep"
TRACE_CRITICAL_CALIFORNIA = "critical_california_ahj_initial_deep"
TRACE_GRIPES_STRICT = "gripes_strict_structured"
TRACE_INTERNAL_COORD_STRICT = "internal_coordination_strict"
TRACE_DEFAULT_STANDARD = "default_standard_reasoning"


@dataclass(frozen=True)
class VerificationRoutingDecision:
    """The full routing decision for a single verification call.

    Every policy knob the verification request builder reads lives here,
    so a downstream caller (real-time, batch initial, batch retry, batch
    continuation) cannot pick a different policy than the selector
    intended.

    Attributes
    ----------
    finding_id:
        Stable ``rf-…`` id of the finding this decision describes. Empty
        string when the finding has not been through pipeline dedup yet
        (e.g. unit tests that synthesize findings directly).
    severity:
        Uppercased severity string — ``CRITICAL`` / ``HIGH`` / ``MEDIUM``
        / ``GRIPES``. Empty severities are coerced to ``GRIPES`` so the
        budget lookup has a well-defined row.
    profile / mode:
        The classified :class:`VerificationProfile` and routed
        :class:`VerificationMode`. The mode encodes the policy bundle;
        the profile sets the per-kind search budget ceiling.
    model:
        The Anthropic model id the request will use. Defaults to the
        mode's model unless an explicit override was passed in (operator
        overrides, escalation paths, tests).
    thinking_enabled:
        Whether the request should include the ``thinking`` key. Two
        conditions must be true: the mode policy asks for thinking, AND
        the selected model supports adaptive thinking. The builder
        consults both — the field on the decision is the *intent*, the
        builder is the gate.
    web_search_enabled / web_search_max_uses:
        Whether the request should attach the ``web_search`` server tool
        and, if so, how many uses it gets. ``max_uses`` is computed as
        ``mode_search_budget(mode, profile_ceiling=profile_max_uses(profile, severity))``
        so the profile sets the ceiling and the mode scales within it.
    include_verdict_tool:
        Whether to attach the ``submit_verification_verdict`` custom tool.
        Tied to ``structured_tool_output_enabled()`` at the call site
        (not stamped at decision time because the env var can flip
        between selection and submission).
    cache_phase:
        The :mod:`src.api_config` phase used for cache controls / effort
        / thinking lookup. ``PHASE_VERIFICATION`` for initial calls,
        ``PHASE_VERIFICATION_RETRY`` for retry-wave calls, and
        ``PHASE_VERIFICATION_CONTINUATION`` for ``pause_turn`` resumes.
    max_continuations:
        Cap on the real-time pause-turn / continuation loop. The batch
        wave loop bounds continuations through ``MAX_VERIFICATION_WAVES``
        instead, so this field is unused on that path.
    escalation_eligible:
        Whether a failed result in this mode should trigger a re-run on
        the escalation model. ``False`` for LOCAL_SKIP / STRICT_STRUCTURED
        / DEEP_REASONING; only STANDARD_REASONING is eligible.
    local_skip:
        ``True`` iff the finding short-circuited to a local-skip result.
        When ``True``, callers should NOT build a remote request — the
        decision carries enough info to stamp the local-skip telemetry
        and that is the whole record.
    escalated:
        ``True`` iff this decision is the *second* pass after a failed
        STANDARD_REASONING attempt. Forces ``DEEP_REASONING`` regardless
        of severity / profile.
    trace_reason:
        Short machine-readable tag explaining why this routing was
        selected. See the ``TRACE_*`` constants.
    """

    finding_id: str
    severity: str
    profile: VerificationProfile
    mode: VerificationMode
    model: str
    thinking_enabled: bool
    web_search_enabled: bool
    web_search_max_uses: int
    include_verdict_tool: bool
    cache_phase: str
    max_continuations: int
    escalation_eligible: bool
    local_skip: bool
    escalated: bool
    trace_reason: str

    # -------------------------------------------------------------------
    # Serialization. Returns a JSON-safe dict so the decision can be
    # stashed in ``request_map`` / ``request_contexts`` and round-tripped
    # by the wave parser without depending on pickle or pydantic.
    # -------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "severity": self.severity,
            "profile": self.profile.value,
            "mode": self.mode.value,
            "model": self.model,
            "thinking_enabled": bool(self.thinking_enabled),
            "web_search_enabled": bool(self.web_search_enabled),
            "web_search_max_uses": int(self.web_search_max_uses),
            "include_verdict_tool": bool(self.include_verdict_tool),
            "cache_phase": self.cache_phase,
            "max_continuations": int(self.max_continuations),
            "escalation_eligible": bool(self.escalation_eligible),
            "local_skip": bool(self.local_skip),
            "escalated": bool(self.escalated),
            "trace_reason": self.trace_reason,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VerificationRoutingDecision":
        """Rebuild a decision from a previously-serialized dict.

        Unknown / missing fields fall back to safe defaults so a legacy
        ``request_contexts`` entry (pre-Chunk-4) does not crash the wave
        parser — the caller can detect "no decision was stored" by the
        presence of the ``routing`` key, and use this constructor only
        when the key is present.
        """
        profile_value = payload.get("profile") or VerificationProfile.CONSTRUCTABILITY.value
        try:
            profile = VerificationProfile(profile_value)
        except ValueError:
            profile = VerificationProfile.CONSTRUCTABILITY

        mode_value = payload.get("mode") or VerificationMode.STANDARD_REASONING.value
        try:
            mode = VerificationMode(mode_value)
        except ValueError:
            mode = VerificationMode.STANDARD_REASONING

        return cls(
            finding_id=str(payload.get("finding_id") or ""),
            severity=str(payload.get("severity") or "GRIPES"),
            profile=profile,
            mode=mode,
            model=str(payload.get("model") or ""),
            thinking_enabled=bool(payload.get("thinking_enabled", False)),
            web_search_enabled=bool(payload.get("web_search_enabled", True)),
            web_search_max_uses=int(payload.get("web_search_max_uses", 0)),
            include_verdict_tool=bool(payload.get("include_verdict_tool", True)),
            cache_phase=str(payload.get("cache_phase") or PHASE_VERIFICATION),
            max_continuations=int(
                payload.get("max_continuations", _DEFAULT_MAX_CONTINUATIONS)
            ),
            escalation_eligible=bool(payload.get("escalation_eligible", False)),
            local_skip=bool(payload.get("local_skip", False)),
            escalated=bool(payload.get("escalated", False)),
            trace_reason=str(payload.get("trace_reason") or ""),
        )


# ---------------------------------------------------------------------------
# Selector
# ---------------------------------------------------------------------------


def _trace_for(
    *,
    local_skip: bool,
    cached_mode: VerificationMode | str | None,
    escalated: bool,
    mode: VerificationMode,
) -> str:
    """Pick the short trace tag that explains *why* this mode was chosen."""
    if local_skip:
        return TRACE_LOCAL_SKIP
    if cached_mode is not None:
        return TRACE_CACHED_MODE
    if escalated:
        return TRACE_ESCALATED
    if mode is VerificationMode.DEEP_REASONING:
        return TRACE_CRITICAL_CALIFORNIA
    if mode is VerificationMode.STRICT_STRUCTURED:
        # The router only routes to STRICT_STRUCTURED via two branches:
        # GRIPES (any non-internal-coord profile), or non-GRIPES
        # internal-coord. The distinction matters for diagnostics
        # because the two have different policy implications, so the
        # trace splits them.
        return TRACE_GRIPES_STRICT
    return TRACE_DEFAULT_STANDARD


def select_routing(
    finding: Finding | None,
    *,
    escalated: bool = False,
    cached_mode: VerificationMode | str | None = None,
    model_override: str | None = None,
    cache_phase: str = PHASE_VERIFICATION,
    max_continuations: int | None = None,
    local_skip: bool | None = None,
    include_verdict_tool: bool | None = None,
) -> VerificationRoutingDecision:
    """Produce the full routing decision for a single verification call.

    Pure function over the finding + escalation / cache hints. Side-effect
    free and deterministic given the env (which is read through the
    verification-router / verification-modes helpers).

    Parameters
    ----------
    finding:
        The finding to verify. ``None`` is accepted for unit-test
        convenience and routes to STANDARD_REASONING with empty fields.
    escalated:
        ``True`` iff this call is the second pass after a failed initial
        attempt. Forces DEEP_REASONING.
    cached_mode:
        When a cache hit replays a prior verdict, the caller passes the
        stored mode so the returned decision preserves the original
        routing tag instead of being relabeled.
    model_override:
        Operator / test override for the model. Wins over the mode's
        default. ``thinking_enabled`` is still subject to the model's
        capability gate so an override to Haiku (which does not support
        adaptive thinking) cleanly omits the ``thinking`` key.
    cache_phase:
        ``PHASE_VERIFICATION`` / ``PHASE_VERIFICATION_RETRY`` /
        ``PHASE_VERIFICATION_CONTINUATION``. The decision stamps this so
        the request builder reaches for the matching phase policy
        without the caller having to thread it through separately.
    max_continuations:
        Cap on the real-time pause-turn loop. ``None`` (the default)
        lets the selector pick the cap from
        :func:`retry_policy.max_continuations_for_mode` based on the
        routed mode — 2 for everything except DEEP_REASONING, which
        gets 4. Explicit overrides (e.g. tests) still win.
    local_skip:
        When ``True``, the caller has *already* determined the finding
        is a local-skip and is materializing the decision purely for
        telemetry. When ``None`` (the default), the selector consults
        :func:`classify_finding_for_verification` itself. Callers that
        run their own classifier (e.g. ``prepare_findings_for_verification``)
        should pass an explicit ``False`` so the selector does not
        re-run the classifier on the remote path.
    include_verdict_tool:
        Whether to attach ``submit_verification_verdict`` to the request.
        ``None`` defers to :func:`verification_request_includes_verdict_tool`
        at request-build time so an env toggle that flips between
        selection and submission still produces a consistent request.

    The trace_reason field is the short tag explaining which router
    branch fired; ``TRACE_*`` constants document the full set.
    """
    if local_skip is None and finding is not None:
        local_skip = (
            local_skip_enabled()
            and classify_finding_for_verification(finding) == "local_skip"
        )
    elif local_skip is None:
        local_skip = False

    if finding is None:
        # Pure-defensive fallback. Real callers always have a finding;
        # tests sometimes synthesize a "what would routing pick for nothing"
        # check, and the answer should be STANDARD_REASONING with empty
        # identity fields.
        severity = "GRIPES"
        profile = VerificationProfile.CONSTRUCTABILITY
        finding_id = ""
    else:
        severity = (finding.severity or "").strip().upper() or "GRIPES"
        profile = classify_finding_profile(finding)
        finding_id = finding.finding_id or ""

    # Selecting the mode itself is delegated to :mod:`verification_modes`
    # so the priority order (local_skip → escalated → critical-AHJ → GRIPES
    # → internal-coord → default) stays in one place. The router applies
    # the rules in the documented order.
    mode = select_verification_mode(
        finding,
        local_skip=local_skip,
        escalated=escalated,
        cached_mode=cached_mode,
    )
    policy: ModePolicy = mode_policy(mode)

    # Model: operator/test override wins, otherwise the mode's model.
    # Falls back to ``initial_verification_model()`` for the (rare) case
    # where the mode policy returns an empty string — same defensive
    # fallback the legacy ``_run_verification_call`` used.
    if model_override:
        selected_model = model_override
    else:
        selected_model = policy.model or initial_verification_model()

    # Thinking: mode opts out OR model does not support adaptive thinking.
    # Both gates are required so an operator override to Haiku does not
    # crash the request build. The actual ``thinking`` key only lands on
    # the request when ``apply_thinking_config`` agrees, but the decision
    # records the intent.
    thinking_enabled = (
        policy.thinking_enabled and model_supports_adaptive_thinking(selected_model)
    )

    # Search budget: profile sets the per-kind ceiling, mode scales within
    # it. Floor-of-1 inside :func:`mode_search_budget` ensures a non-zero
    # multiplier always grants at least one search.
    profile_ceiling = profile_max_uses(profile, severity)
    max_uses = mode_search_budget(mode, profile_ceiling=profile_ceiling)

    # Tool inclusion: defer to the env-gated helper at request-build time
    # unless the caller passed an explicit override. Storing ``None`` here
    # would leak through to the dict-form, so we resolve to a bool now
    # using the env helper. Importing locally avoids a cycle through
    # :mod:`batch`.
    if include_verdict_tool is None:
        from .batch import verification_request_includes_verdict_tool
        include_verdict_tool = verification_request_includes_verdict_tool()

    trace_reason = _trace_for(
        local_skip=local_skip,
        cached_mode=cached_mode,
        escalated=escalated,
        mode=mode,
    )

    # Chunk 6: derive the per-mode continuation cap from the centralized
    # policy when the caller did not override it. Default modes get 2
    # (drops from the legacy 5); DEEP_REASONING gets 4 so a legitimate
    # CRITICAL CALIFORNIA_AHJ finding still has room to converge.
    if max_continuations is None:
        max_continuations = max_continuations_for_mode(mode.value)

    return VerificationRoutingDecision(
        finding_id=finding_id,
        severity=severity,
        profile=profile,
        mode=mode,
        model=selected_model,
        thinking_enabled=thinking_enabled,
        web_search_enabled=policy.web_search_enabled,
        web_search_max_uses=max_uses,
        include_verdict_tool=bool(include_verdict_tool),
        cache_phase=cache_phase,
        max_continuations=max_continuations,
        escalation_eligible=policy.allows_escalation,
        local_skip=local_skip,
        escalated=escalated,
        trace_reason=trace_reason,
    )


# ---------------------------------------------------------------------------
# Request builder
# ---------------------------------------------------------------------------


def build_verification_tools_from_decision(
    decision: VerificationRoutingDecision,
) -> list[dict]:
    """Build the tool list for a verification request from a routing decision.

    Routes through :func:`src.batch.build_verification_tools_for_profile`
    so the profile-aware web_search budget is used, then patches
    ``max_uses`` with the mode-scaled value from the decision. Adding
    the verdict tool is controlled by the decision's
    ``include_verdict_tool`` field rather than re-querying the env
    helper, so a decision built with one value remains internally
    consistent even if the env toggle flips mid-flight.
    """
    # Local import — :mod:`batch` already depends on :mod:`verifier`, and
    # :mod:`verifier` will depend on this module, so importing batch at
    # module load would form a cycle.
    from .batch import build_verification_tools_for_profile
    from .structured_schemas import verification_verdict_tool

    tool_list = build_verification_tools_for_profile(
        decision.profile, decision.severity
    )
    # Drop the verdict tool when the decision says not to attach it. The
    # profile-aware builder always appends it when
    # ``verification_request_includes_verdict_tool()`` is True at its call
    # site; we strip here so the decision is the final authority.
    web_tool_list: list[dict] = []
    verdict_tool: dict | None = None
    for tool in tool_list:
        if tool.get("type", "").startswith("web_search") or tool.get("name", "").startswith("web_search"):
            web_tool_list.append(dict(tool))
        else:
            verdict_tool = tool

    # Mode-scaled max_uses. The real-time path already overwrote this; the
    # batch path used to ignore it. Centralizing here closes that gap.
    if web_tool_list and decision.web_search_max_uses != web_tool_list[0].get("max_uses"):
        web_tool_list[0]["max_uses"] = decision.web_search_max_uses

    out: list[dict] = list(web_tool_list)
    if decision.include_verdict_tool:
        # If the profile builder produced a verdict tool, keep it; otherwise
        # synthesize one. This handles the rare case where the env flag was
        # off when the profile builder ran but the decision was built
        # asking for the verdict tool.
        out.append(verdict_tool if verdict_tool is not None else verification_verdict_tool())
    return out


def build_verification_request(
    decision: VerificationRoutingDecision,
    *,
    prompt: str,
    system_prompt: str,
    assistant_content: list | None = None,
    include_service_tier: bool = False,
) -> dict[str, Any]:
    """Build the kwargs dict for a single verification request.

    Chunk 4 invariant: every verification request (real-time initial,
    batch initial, batch retry, batch continuation) routes through this
    function. The decision encodes the policy; the rendered ``prompt``
    and ``system_prompt`` flow in from the caller (the verifier already
    has the per-finding prompt builder and the system-prompt builder).

    Parameters
    ----------
    decision:
        The routing decision. Read-only — the builder does not mutate
        it. A ``decision.local_skip == True`` is a caller bug; this
        function is only meaningful for remote requests.
    prompt:
        The per-finding user message text.
    system_prompt:
        The verifier system prompt text.
    assistant_content:
        For ``pause_turn`` continuation resumes, the prior assistant
        content blocks. ``None`` for initial / retry requests.
    include_service_tier:
        Whether to attach the batch ``service_tier`` field. ``True`` for
        batch submissions; ``False`` for real-time streaming (the API
        rejects ``service_tier`` on streaming requests). Defaults to
        ``False`` because the real-time path is the more common caller.
    """
    if decision.local_skip:
        raise ValueError(
            "build_verification_request was called for a local-skip decision; "
            "local-skip callers must short-circuit before the request build."
        )

    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    if assistant_content is not None:
        messages.append({"role": "assistant", "content": assistant_content})

    tools = build_verification_tools_from_decision(decision)

    # Phase-aware cache wrapping — the cache helpers consult the registry
    # entry for ``decision.cache_phase`` so the verification retry /
    # continuation paths reach for the same prefix the initial call
    # cached. Each phase has its own row in the cache policy registry,
    # so a future tuning pass that wants the retry path to skip caching
    # touches one map.
    system_payload = system_prompt_with_cache(system_prompt, phase=decision.cache_phase)
    tools_payload = tools_with_cache(tools, phase=decision.cache_phase)

    params: dict[str, Any] = {
        "model": decision.model,
        "max_tokens": verification_max_tokens(model=decision.model, phase=decision.cache_phase),
        "system": system_payload,
        "tools": tools_payload,
        "messages": messages,
    }
    # Thinking: the decision encodes the intent (mode policy AND model
    # capability). The helper still applies the per-phase no-thinking
    # opt-out for triage, but the verification phases are all eligible
    # so the gate here is the union of (mode allows, model supports).
    if decision.thinking_enabled:
        apply_thinking_config(params, model=decision.model, phase=decision.cache_phase)
    # Effort: paired with thinking per Chunk D1.2. The helper is model-
    # aware and phase-aware on its own; we always call it (the helper
    # omits ``output_config`` for unsupported models).
    apply_effort_config(params, model=decision.model, phase=decision.cache_phase)

    if include_service_tier:
        tier = batch_service_tier()
        if tier:
            params["service_tier"] = tier

    return params


# ---------------------------------------------------------------------------
# Result stamping
# ---------------------------------------------------------------------------


def apply_routing_to_result(
    decision: VerificationRoutingDecision,
    result,
) -> None:
    """Stamp the routed mode / profile / escalation flag onto a result.

    Used by both the real-time path (after the verifier call returns)
    and the batch wave parser (which reconstructs the decision from
    ``request_contexts`` so it stamps the SAME decision the request was
    built with, instead of re-deriving from the finding).

    The actual ``escalated`` flag is taken from the decision rather than
    re-computed — the wave parser used to re-derive it from the wave
    metadata, which is correct but redundant. Centralizing here means
    a future stat (like ``initial_model`` / ``initial_verdict``) can be
    threaded through the decision without touching two stamping sites.

    Mutates ``result`` in place; returns nothing because the caller
    always already has a reference.
    """
    if result is None:
        return
    result.verification_profile = decision.profile.value
    result.verification_mode = decision.mode.value
    # ``escalated`` is the runtime "this result came from Opus" flag. The
    # decision records the same fact for callers that want to inspect the
    # routing without consulting the result.
    result.escalated = decision.escalated


__all__ = [
    "TRACE_LOCAL_SKIP",
    "TRACE_LOCAL_SKIP_BYPASSED",
    "TRACE_CACHED_MODE",
    "TRACE_ESCALATED",
    "TRACE_CRITICAL_CALIFORNIA",
    "TRACE_GRIPES_STRICT",
    "TRACE_INTERNAL_COORD_STRICT",
    "TRACE_DEFAULT_STANDARD",
    "VerificationRoutingDecision",
    "select_routing",
    "build_verification_request",
    "build_verification_tools_from_decision",
    "apply_routing_to_result",
]
