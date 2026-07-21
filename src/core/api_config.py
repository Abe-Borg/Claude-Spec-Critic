"""Centralized Anthropic API configuration for Spec Critic.

Single place for model identifiers, per-phase output-token caps, batch
beta headers, web-search tool configuration, and request-shape policy
(prompt caching, adaptive thinking, effort).

Model identifiers may be overridden via env vars:
    SPEC_CRITIC_REVIEW_MODEL                — review (default Opus 4.8).
    SPEC_CRITIC_VERIFICATION_MODEL          — verification initial pass
                                              (default Sonnet 5).
    SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL — escalation (default Opus 4.8).
    SPEC_CRITIC_TRIAGE_MODEL                — verification triage
                                              (default Haiku 4.5).
    SPEC_CRITIC_RESEARCH_MODEL              — requirements research fan-out
                                              (default Sonnet 5).
    SPEC_CRITIC_DRAWING_DIGEST_MODEL        — construction-drawing digest
                                              vision pass (default Sonnet 5).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Iterable

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model identifiers (centralized)
# ---------------------------------------------------------------------------

MODEL_OPUS_48 = "claude-opus-4-8"
MODEL_SONNET_5 = "claude-sonnet-5"
# Previous-generation Sonnet. Kept registered (constant + capability entry)
# so operator env overrides that pin it keep their correct request shape —
# most notably the ``xhigh`` → ``high`` effort clamp, which 4.6 needs and
# Sonnet 5 does not.
MODEL_SONNET_46 = "claude-sonnet-4-6"
MODEL_HAIKU_45 = "claude-haiku-4-5"

# Review runs on the current Opus flagship; verification routes through
# Sonnet first and reserves Opus for escalation on CRITICAL/HIGH UNVERIFIED
# findings. Defaults track the newest generation of each tier (Opus 4.8 /
# Sonnet 5). Override any of these via the matching ``SPEC_CRITIC_*_MODEL``
# env var.
REVIEW_MODEL_DEFAULT = os.environ.get("SPEC_CRITIC_REVIEW_MODEL", MODEL_OPUS_48)
CROSS_CHECK_MODEL_DEFAULT = MODEL_SONNET_5
VERIFICATION_MODEL_DEFAULT = os.environ.get(
    "SPEC_CRITIC_VERIFICATION_MODEL", MODEL_SONNET_5
)

# Model used when escalating a low-confidence/high-severity verification.
VERIFICATION_ESCALATION_MODEL = os.environ.get(
    "SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL", MODEL_OPUS_48
)

# Verification triage pre-pass (triage.classify_findings_with_haiku) decides
# whether a finding can be locally resolved or needs web verification. The
# task is shallow classification over short inputs; Haiku fits.
TRIAGE_MODEL_DEFAULT = os.environ.get("SPEC_CRITIC_TRIAGE_MODEL", MODEL_HAIKU_45)

# Requirements-research fan-out (per-dimension web_search calls that build
# the Project Requirements Profile for profile-enabled modules). Sonnet:
# the task is retrieval + structured summarization, not deep review.
RESEARCH_MODEL_DEFAULT = os.environ.get("SPEC_CRITIC_RESEARCH_MODEL", MODEL_SONNET_5)

# Local-code compliance pass (profile-enabled modules; modeled on
# cross-check). Bound directly to Sonnet with NO env override — deliberate
# parity with ``CROSS_CHECK_MODEL_DEFAULT``, which is likewise unswappable.
COMPLIANCE_MODEL_DEFAULT = MODEL_SONNET_5

# Construction-drawing digest (one-time vision pass at attach time that
# turns drawing PDFs into a plain-text Project Context block). Sonnet: the
# task is transcription + structured summarization of provided documents,
# not deep review. All four whitelisted models accept PDF document blocks
# (there is no ``supports_vision`` capability flag — a non-vision override
# fails fast at the digest call itself, before anything downstream is
# billed).
DRAWING_DIGEST_MODEL_DEFAULT = os.environ.get(
    "SPEC_CRITIC_DRAWING_DIGEST_MODEL", MODEL_SONNET_5
)

# Drawing-impact synthesis (one post-review pass that explains, for the
# report, how the attached construction drawings informed the review — it
# cross-references the final findings against the drawing digest already in
# Project Context). Sonnet: the task is grounded synthesis over text the run
# already produced, not deep review. Env-overridable like the digest it
# reads, defaulting to the same tier.
DRAWING_IMPACT_MODEL_DEFAULT = os.environ.get(
    "SPEC_CRITIC_DRAWING_IMPACT_MODEL", MODEL_SONNET_5
)


# Opus family membership now drives exactly one policy decision: the
# verification-phase effort bump (Opus on a verification phase is always the
# escalation tier ⇒ effort ``high``). Output ceilings resolve through the
# capability whitelist (``model_capabilities(model).max_output_tokens``), and
# the ``xhigh`` effort gate is the per-model ``supports_xhigh_effort`` flag —
# neither depends on this set anymore, so a new Opus id missing from it can
# no longer be silently clamped to a smaller output cap.
OPUS_MODELS = frozenset({MODEL_OPUS_48})
HAIKU_MODELS = frozenset({MODEL_HAIKU_45})

# Models whose vision tier is the high-resolution one (2576px long edge,
# ~4784-token image cap). Sonnet 5 is the first Sonnet-tier model with
# high-res image support, so this can't be OPUS_MODELS anymore. Consumed by
# ``tokenizer._image_caps_for_model`` for image-token cost estimates.
HIRES_VISION_MODELS = frozenset({MODEL_OPUS_48, MODEL_SONNET_5})


# ---------------------------------------------------------------------------
# Output-token caps
# ---------------------------------------------------------------------------

# Hard ceilings imposed by the model.
MAX_OUTPUT_TOKENS_OPUS = 128_000
MAX_OUTPUT_TOKENS_SONNET_5 = 128_000  # Sonnet 5 matches the Opus ceiling
MAX_OUTPUT_TOKENS_SONNET = 64_000     # Sonnet 4.6 (previous generation)
MAX_OUTPUT_TOKENS_HAIKU = 64_000

# Extended-output batch beta. Required header to use 300k output in batch.
BATCH_OUTPUT_BETA = "output-300k-2026-03-24"
BATCH_MAX_OUTPUT_TOKENS = 300_000

# Per-phase dynamic caps. These are intentionally lower than the hard model
# ceilings so the app does not blanket-allocate the maximum on every call.
# A single review baseline keeps findings consistent on normal-size specs.
# (Anthropic bills by actual output, so the cap is a fail-fast guard, not a
# cost lever.) The extended 300k path is gated behind the
# ``output-300k-2026-03-24`` beta header for large batch inputs only.
REVIEW_OUTPUT_CAP = 128_000              # baseline review cap
REVIEW_OUTPUT_CAP_BATCH_EXTENDED = 300_000  # batch-only, with 300k beta header
CROSS_CHECK_OUTPUT_CAP = 96_000       # cross-check needs more than verify
# Verdicts are 1-2 sentences per the verifier system prompt; 16k is a
# fail-fast guard, not a billing knob (you pay only for actual output).
VERIFICATION_OUTPUT_CAP = 16_000
# Triage emits a small array of {index, classification, reason}; 8k is more
# than enough even for a 50-finding chunk.
HAIKU_TRIAGE_OUTPUT_CAP = 8_000
# One research dimension returns a structured item list plus tool-use /
# thinking overhead. Field measurement (hyperscale DC plan, D-11 [FT]):
# dimension outputs ran 6–14k tokens before protocol overhead, so the
# original 16k-style verification cap would truncate the heavy dimensions.
RESEARCH_OUTPUT_CAP = 24_000
# The compliance pass emits a coverage matrix (one row per profile
# requirement) plus findings — cross-check-scale output, sized between the
# cross-check (96k) and verification (16k) caps.
COMPLIANCE_OUTPUT_CAP = 64_000
# One drawing-digest chunk targets ~12k tokens of digest text (the in-prompt
# length contract); 24k gives headroom for notes-dense sheets without
# inviting rambling. Anything larger would let a 4-chunk digest exceed the
# 100k PROJECT_CONTEXT_MAX_TOKENS cap on its own.
DRAWING_DIGEST_OUTPUT_CAP = 24_000
# The drawing-impact synthesis emits a short narrative plus a bounded list of
# per-finding links (only the findings the drawings actually bear on), so its
# output is naturally small — 16k is a fail-fast guard, not a billing knob.
DRAWING_IMPACT_OUTPUT_CAP = 16_000

# Token threshold above which a review uses the larger batch cap.
LARGE_REVIEW_INPUT_THRESHOLD = 200_000


# Phase identifiers. Defined here (before the phase→budget registry) so
# the registry can reference them directly. ``thinking_config_for`` and
# ``apply_thinking_config`` further below also consume these.
PHASE_REVIEW = "review"
PHASE_CROSS_CHECK = "cross_check"
PHASE_VERIFICATION = "verification"
PHASE_VERIFICATION_RETRY = "verification_retry"
PHASE_VERIFICATION_CONTINUATION = "verification_continuation"
PHASE_TRIAGE = "triage"
PHASE_RESEARCH = "research"
PHASE_COMPLIANCE = "compliance"
PHASE_DRAWING_DIGEST = "drawing_digest"
PHASE_DRAWING_IMPACT = "drawing_impact"


def output_cap_for_model(model: str, *, requested: int) -> int:
    """Clamp ``requested`` to the model's hard output ceiling.

    Resolves through the capability whitelist so the ceiling and the rest of
    the model's request-shape policy come from one registry — the legacy
    family-set dispatch (``model in OPUS_MODELS``) silently clamped any
    128k-capable model that wasn't an Opus id (e.g. Sonnet 5) down to the
    64k previous-generation-Sonnet ceiling. Unknown ids still resolve to the
    conservative 64k default (and warn once via ``model_capabilities``).
    """
    return min(requested, model_capabilities(model).max_output_tokens)


# Single registry of per-phase output budgets so verification
# retry/continuation and triage all resolve through the same lookup. Each
# phase declares its desired cap; ``phase_output_cap`` clamps that to the
# selected model's ceiling. The phase helpers below stay as thin wrappers
# so callers can keep their existing imports.
#
# Verification retry/continuation reuse the verification cap by default —
# the verdict envelope is unchanged across retries, so granting more output
# only invites the model to ramble. If a future investigation shows
# continuations need more headroom, this is the one place to tune it.
_PHASE_OUTPUT_BUDGET: dict[str, int] = {
    PHASE_REVIEW: REVIEW_OUTPUT_CAP,
    PHASE_CROSS_CHECK: CROSS_CHECK_OUTPUT_CAP,
    PHASE_VERIFICATION: VERIFICATION_OUTPUT_CAP,
    PHASE_VERIFICATION_RETRY: VERIFICATION_OUTPUT_CAP,
    PHASE_VERIFICATION_CONTINUATION: VERIFICATION_OUTPUT_CAP,
    PHASE_TRIAGE: HAIKU_TRIAGE_OUTPUT_CAP,
    PHASE_RESEARCH: RESEARCH_OUTPUT_CAP,
    PHASE_COMPLIANCE: COMPLIANCE_OUTPUT_CAP,
    PHASE_DRAWING_DIGEST: DRAWING_DIGEST_OUTPUT_CAP,
    PHASE_DRAWING_IMPACT: DRAWING_IMPACT_OUTPUT_CAP,
}


def phase_output_cap(phase: str, *, model: str) -> int:
    """Return the centralized per-phase max_tokens budget for ``model``.

    Every phase resolves its output cap here so review, batch review,
    cross-check, verification, verification retry, verification continuation,
    and triage all share one registry. Unknown phases fall back to the
    verification cap, the most conservative value in the registry — a future
    phase that forgets to register loses headroom instead of accidentally
    inheriting the 128k review cap.
    """
    requested = _PHASE_OUTPUT_BUDGET.get(phase, VERIFICATION_OUTPUT_CAP)
    return output_cap_for_model(model, requested=requested)


def triage_max_tokens(*, model: str = TRIAGE_MODEL_DEFAULT) -> int:
    return phase_output_cap(PHASE_TRIAGE, model=model)


def review_max_tokens(*, model: str = REVIEW_MODEL_DEFAULT, allow_extended_output: bool = False) -> int:
    """Return a per-call max_tokens for a review request.

    Both review transports share the same baseline cap on normal-size
    specs (the batch path and the real-time streaming path build through
    the same request builder). ``allow_extended_output`` selects the 300k
    batch-only path — the real-time transport always pins it off — and the
    beta header is checked at the call site by
    :func:`assert_extended_output_allowed`.
    """
    if allow_extended_output:
        return min(BATCH_MAX_OUTPUT_TOKENS, REVIEW_OUTPUT_CAP_BATCH_EXTENDED)
    return phase_output_cap(PHASE_REVIEW, model=model)


# Real-time review fan-out concurrency. Review streams are the app's heaviest
# synchronous calls (xhigh effort, up to the 128k phase cap of output — 5-8x
# the output budget of any other streaming phase), so the default pool is
# aligned with the research fan-out (4) while remaining below the verification
# real-time fallback (5): four concurrent streams keep a multi-spec run moving
# without immediately jumping to the app's maximum pressure on lower API tiers
# (429s are retryable, but a storm burns the retry budget and surfaces as
# failed-review specs). GUI runs pass their persisted 2/4/6/8 choice
# explicitly; headless callers can tune the environment variable.
ENV_REALTIME_REVIEW_WORKERS = "SPEC_CRITIC_REALTIME_REVIEW_WORKERS"
REALTIME_REVIEW_MAX_WORKERS_DEFAULT = 4
REALTIME_REVIEW_WORKER_CHOICES = (2, 4, 6, 8)
_REALTIME_REVIEW_WORKERS_CEILING = max(REALTIME_REVIEW_WORKER_CHOICES)


def normalize_realtime_review_workers(value: object) -> int:
    """Clamp a programmatic worker selection to the supported runtime range."""

    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return REALTIME_REVIEW_MAX_WORKERS_DEFAULT
    return max(1, min(_REALTIME_REVIEW_WORKERS_CEILING, parsed))


def realtime_review_max_workers() -> int:
    """Concurrent streaming-review workers for the real-time transport.

    Reads ``SPEC_CRITIC_REALTIME_REVIEW_WORKERS`` fresh on each call (test
    seam; no import-order surprises), clamps to [1, 8], and falls back to
    the default (4) on a missing or malformed value so a typo never
    serializes — or stampedes — a run.
    """
    raw = os.environ.get(ENV_REALTIME_REVIEW_WORKERS)
    if raw is None or not raw.strip():
        return REALTIME_REVIEW_MAX_WORKERS_DEFAULT
    return normalize_realtime_review_workers(raw.strip())


def _bounded_worker_env(name: str, *, default: int, ceiling: int) -> int:
    """Read a positive, bounded worker count without import-time caching."""

    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return max(1, min(ceiling, value))


# Program-level concurrency.  These caps are deliberately separate from the
# per-request model settings above: routed programs may contain several child
# modules, each of which already has internal fan-out.  The outer scheduler
# must therefore be bounded independently so adding a module never multiplies
# API pressure without limit.
ENV_RESEARCH_WORKERS = "SPEC_CRITIC_RESEARCH_WORKERS"
RESEARCH_MAX_WORKERS_DEFAULT = 4
_RESEARCH_WORKERS_CEILING = 12

ENV_PROGRAM_PREPARE_WORKERS = "SPEC_CRITIC_PROGRAM_PREPARE_WORKERS"
PROGRAM_PREPARE_MAX_WORKERS_DEFAULT = 4
_PROGRAM_PREPARE_WORKERS_CEILING = 8

ENV_PROGRAM_COLLECTION_WORKERS = "SPEC_CRITIC_PROGRAM_COLLECTION_WORKERS"
PROGRAM_COLLECTION_MAX_WORKERS_DEFAULT = 2
_PROGRAM_COLLECTION_WORKERS_CEILING = 4

ENV_REALTIME_COLLECTION_CALLS = "SPEC_CRITIC_REALTIME_COLLECTION_CALLS"
REALTIME_COLLECTION_MAX_CALLS_DEFAULT = 5
_REALTIME_COLLECTION_CALLS_CEILING = 10


def research_max_workers() -> int:
    """Global requirements-research call budget for a routed program."""

    return _bounded_worker_env(
        ENV_RESEARCH_WORKERS,
        default=RESEARCH_MAX_WORKERS_DEFAULT,
        ceiling=_RESEARCH_WORKERS_CEILING,
    )


def program_prepare_max_workers() -> int:
    """Maximum module preparations allowed to overlap in one program run."""

    return _bounded_worker_env(
        ENV_PROGRAM_PREPARE_WORKERS,
        default=PROGRAM_PREPARE_MAX_WORKERS_DEFAULT,
        ceiling=_PROGRAM_PREPARE_WORKERS_CEILING,
    )


def program_collection_max_workers() -> int:
    """Maximum whole-module collection pipelines allowed to overlap."""

    return _bounded_worker_env(
        ENV_PROGRAM_COLLECTION_WORKERS,
        default=PROGRAM_COLLECTION_MAX_WORKERS_DEFAULT,
        ceiling=_PROGRAM_COLLECTION_WORKERS_CEILING,
    )


def realtime_collection_max_calls() -> int:
    """Global synchronous API-call budget during concurrent collection."""

    return _bounded_worker_env(
        ENV_REALTIME_COLLECTION_CALLS,
        default=REALTIME_COLLECTION_MAX_CALLS_DEFAULT,
        ceiling=_REALTIME_COLLECTION_CALLS_CEILING,
    )


def cross_check_max_tokens(*, model: str = CROSS_CHECK_MODEL_DEFAULT) -> int:
    return phase_output_cap(PHASE_CROSS_CHECK, model=model)


def research_max_tokens(*, model: str = RESEARCH_MODEL_DEFAULT) -> int:
    return phase_output_cap(PHASE_RESEARCH, model=model)


def compliance_max_tokens(*, model: str = COMPLIANCE_MODEL_DEFAULT) -> int:
    return phase_output_cap(PHASE_COMPLIANCE, model=model)


def drawing_digest_max_tokens(*, model: str = DRAWING_DIGEST_MODEL_DEFAULT) -> int:
    return phase_output_cap(PHASE_DRAWING_DIGEST, model=model)


def drawing_impact_max_tokens(*, model: str = DRAWING_IMPACT_MODEL_DEFAULT) -> int:
    return phase_output_cap(PHASE_DRAWING_IMPACT, model=model)


def verification_max_tokens(*, model: str = VERIFICATION_MODEL_DEFAULT, phase: str = PHASE_VERIFICATION) -> int:
    """Return a per-call max_tokens for a verification request.

    ``phase`` defaults to ``PHASE_VERIFICATION``; pass
    ``PHASE_VERIFICATION_RETRY`` or ``PHASE_VERIFICATION_CONTINUATION`` to
    pick up retry-specific or continuation-specific budgets from the central
    registry. Today all three resolve to the same cap; the parameter exists
    so a future tuning pass touches one place.
    """
    return phase_output_cap(phase, model=model)


def assert_extended_output_allowed(
    *, max_tokens: int, betas: Iterable[str] | None, model: str | None = None
) -> None:
    """Guard against extended output without the required beta header.

    The Anthropic API rejects output above a model's baseline ceiling when
    the extended-output beta is not set, but the failure surfaces deep in the
    request lifecycle. Plan Sprint 2 item 8: fail fast at the call site instead.

    The threshold is the *selected model's* baseline (non-beta) output ceiling
    (TRUST_AUDIT P2-3), derived from the single :func:`output_cap_for_model`
    source of truth — Opus 128k, Sonnet/Haiku 64k. Passing ``model`` makes the
    guard correct for Sonnet (whose 64k baseline is below the old hardcoded
    128k threshold, so a 64k–128k Sonnet request without the beta would have
    slipped past). When ``model`` is omitted the guard falls back to the
    highest baseline ceiling (Opus 128k) so it never *over*-fires on a
    legitimate sub-ceiling request — the API stays the backstop for that case.
    """
    ceiling = (
        output_cap_for_model(model, requested=BATCH_MAX_OUTPUT_TOKENS)
        if model
        else MAX_OUTPUT_TOKENS_OPUS
    )
    if max_tokens <= ceiling:
        return
    beta_set = set(betas or ())
    if BATCH_OUTPUT_BETA not in beta_set:
        raise ValueError(
            f"Requested max_tokens={max_tokens:,} exceeds the baseline output "
            f"ceiling ({ceiling:,}" + (f" for model '{model}'" if model else "") + ") "
            f"and requires beta header '{BATCH_OUTPUT_BETA}'. Refusing to submit without it."
        )


# ---------------------------------------------------------------------------
# Model capability policy
# ---------------------------------------------------------------------------
#
# Whitelist-style registry of per-model capabilities. The Anthropic API
# rejects requests that include feature parameters the selected model does
# not support — most notably ``thinking`` against Haiku 4.5, which produces
# an API error.
#
# To add a new model: register it in ``_MODEL_CAPABILITIES``. Unknown model
# IDs fall through to ``_DEFAULT_CAPABILITIES``, which disables every
# capability flag — intentional. Stripping a feature from a future model is
# strictly safer than sending an invalid request that fails deep in the
# request lifecycle. The degradation is no longer silent, though: an
# unrecognized id logs one WARNING (see :func:`model_capabilities`) so a
# stale whitelist that quietly under-powers a newer/better model is visible
# to the operator rather than hidden.


@dataclass(frozen=True)
class ModelCapabilities:
    """Per-model feature support. Drives request-shape decisions."""

    supports_adaptive_thinking: bool
    max_output_tokens: int
    supports_extended_output_beta: bool  # 300k batch-only beta header
    context_window: int
    # Whether the model accepts ``output_config.effort``. The
    # parameter controls token eagerness and tool-call behavior. Sending
    # it to an unsupported model returns an API error, so the policy in
    # :func:`effort_config_for` must consult this flag before attaching
    # the field. Default ``False`` so unknown models silently omit it.
    supports_effort: bool = False
    # Whether the model accepts ``strict: true`` on custom tool definitions
    # (structured outputs / strict tool use). Anthropic documents the
    # feature for specific models; sending it to one outside that set risks
    # a 400 at submit. The tool builders in ``structured_schemas`` consult
    # this flag, so a ``SPEC_CRITIC_*_MODEL`` override to an
    # unlisted-but-valid model degrades to the lenient tool shape instead
    # of an API rejection. Default ``False``.
    supports_strict_tools: bool = False
    # Whether the model accepts ``output_config.effort: "xhigh"``. Opus 4.8
    # and Sonnet 5 do; Sonnet 4.6's supported set is {low, medium, high,
    # max} and it rejects ``xhigh`` at submit with a 400. Consulted by
    # ``_clamp_effort_for_model`` — a phase that defaults to ``xhigh`` on a
    # model without this flag clamps down to ``high`` instead of erroring.
    # Default ``False`` so unknown models take the safe clamp.
    supports_xhigh_effort: bool = False


_MODEL_CAPABILITIES: dict[str, ModelCapabilities] = {
    MODEL_OPUS_48: ModelCapabilities(
        # Claude Opus 4.8 capability profile per Anthropic's "What's new in
        # Claude Opus 4.8" and the models overview: 1M-token context window on
        # the Claude API, 128k max output, the ``output-300k-2026-03-24`` batch
        # beta (shared with Sonnet 4.6), extended/adaptive thinking, and the
        # ``effort`` parameter (default high). Registered explicitly so
        # selecting it via ``SPEC_CRITIC_*_MODEL`` unlocks full capabilities
        # instead of falling through to the conservative unknown-model defaults.
        supports_adaptive_thinking=True,
        max_output_tokens=MAX_OUTPUT_TOKENS_OPUS,
        supports_extended_output_beta=True,
        context_window=1_000_000,
        supports_effort=True,
        supports_strict_tools=True,
        supports_xhigh_effort=True,
    ),
    MODEL_SONNET_5: ModelCapabilities(
        # Claude Sonnet 5 capability profile per Anthropic's models overview
        # and the Sonnet 5 migration guide: adaptive thinking (on by default
        # when the field is omitted — this app always sends it explicitly),
        # 1M-token context window, a 128k output ceiling (first Sonnet at
        # the Opus ceiling), the full effort range INCLUDING ``xhigh``
        # (first Sonnet-tier model with it), and structured outputs /
        # strict tool use. The 300k batch extended-output beta is left off
        # pending confirmation against the beta's supported-model list —
        # same conservative start Sonnet 4.6 had before its rollout was
        # confirmed; the only cost is a 128k baseline cap on a
        # Sonnet-overridden extended review, never an API rejection.
        supports_adaptive_thinking=True,
        max_output_tokens=MAX_OUTPUT_TOKENS_SONNET_5,
        supports_extended_output_beta=False,
        context_window=1_000_000,
        supports_effort=True,
        supports_strict_tools=True,
        supports_xhigh_effort=True,
    ),
    MODEL_SONNET_46: ModelCapabilities(
        supports_adaptive_thinking=True,
        max_output_tokens=MAX_OUTPUT_TOKENS_SONNET,
        # Sonnet 4.6 supports the ``output-300k-2026-03-24`` beta
        # on Message Batches. The prior ``False`` value predated that
        # capability rollout and forced the batch path to gate extended
        # output by Opus-only family membership.
        supports_extended_output_beta=True,
        context_window=1_000_000,
        supports_effort=True,
        supports_strict_tools=True,
        # Sonnet 4.6 rejects ``xhigh`` (400: "This model does not support
        # effort level 'xhigh'") — the clamp to ``high`` stays load-bearing
        # for any env override that pins this previous-generation id.
        supports_xhigh_effort=False,
    ),
    MODEL_HAIKU_45: ModelCapabilities(
        # Anthropic models overview lists Haiku 4.5 without adaptive
        # thinking support; sending ``thinking`` to it returns an API error.
        supports_adaptive_thinking=False,
        max_output_tokens=MAX_OUTPUT_TOKENS_HAIKU,
        supports_extended_output_beta=False,
        context_window=200_000,
        # The Anthropic effort docs list Haiku 4.5 without effort support.
        # Omit ``output_config.effort`` for Haiku to keep request shapes
        # safe across model swaps (e.g. triage).
        supports_effort=False,
        # Structured outputs / strict tool use is documented for Haiku 4.5.
        supports_strict_tools=True,
    ),
}


# Unknown models: every capability flag defaults to False so we never
# construct an invalid request payload. Output cap defaults to the Sonnet
# ceiling, the most conservative of the supported models that still leaves
# room for a meaningful response.
_DEFAULT_CAPABILITIES = ModelCapabilities(
    supports_adaptive_thinking=False,
    max_output_tokens=MAX_OUTPUT_TOKENS_SONNET,
    supports_extended_output_beta=False,
    context_window=200_000,
    supports_effort=False,
    supports_strict_tools=False,
)


# Falling through to ``_DEFAULT_CAPABILITIES`` keeps a misconfigured
# ``SPEC_CRITIC_*_MODEL`` from constructing an invalid request, but the
# degradation used to be *silent*: an operator who pinned a newer/better model
# than the whitelist knew about got quietly smaller requests (no extended
# thinking, no effort tuning, a 64k output cap instead of 128k/300k, a 200k
# context window instead of 1M, no batch extended-output beta) with no signal
# anywhere. We now emit one WARNING per unrecognized id so the quality loss is
# visible. Deduped via a module-level set because ``model_capabilities`` sits
# on a per-request hot path and must not spam the log.
_WARNED_UNKNOWN_MODELS: set[str] = set()


def _warn_unknown_model(model: str) -> None:
    """Emit a one-time WARNING that ``model`` fell through to safe defaults."""
    if model in _WARNED_UNKNOWN_MODELS:
        return
    _WARNED_UNKNOWN_MODELS.add(model)
    _log.warning(
        "Model id %r is not in the capability whitelist (_MODEL_CAPABILITIES "
        "in src/core/api_config.py); degrading to conservative defaults: no "
        "adaptive thinking, no effort tuning, %s-token output cap, %s-token "
        "context window, no 300k extended-output beta, no strict tool use. "
        "If this is a "
        "known-good model, add it to the whitelist to unlock its full "
        "capabilities.",
        model,
        f"{_DEFAULT_CAPABILITIES.max_output_tokens:,}",
        f"{_DEFAULT_CAPABILITIES.context_window:,}",
    )


def model_capabilities(model: str) -> ModelCapabilities:
    """Return the capability record for ``model`` (or safe defaults).

    Known ids resolve from ``_MODEL_CAPABILITIES``. Unknown ids fall through
    to ``_DEFAULT_CAPABILITIES`` *and* trigger a one-time WARNING (see
    :func:`_warn_unknown_model`) so the conservative degradation is never
    silent — the failure mode the trust audit (P0-3) flagged, where a
    deliberately-selected newer model gets quietly worse requests.
    """
    caps = _MODEL_CAPABILITIES.get(model)
    if caps is not None:
        return caps
    _warn_unknown_model(model)
    return _DEFAULT_CAPABILITIES


def model_supports_adaptive_thinking(model: str) -> bool:
    """Whether ``model`` accepts the ``thinking`` request parameter."""
    return model_capabilities(model).supports_adaptive_thinking


def model_supports_effort(model: str) -> bool:
    """Whether ``model`` accepts the ``output_config.effort`` parameter.

    Callers MUST check this before attaching
    ``output_config={"effort": ...}`` to a request. Unsupported models
    (Haiku 4.5, unknown / future models) silently omit the field — the
    field is opt-in per model, so omitting it is always safe.
    """
    return model_capabilities(model).supports_effort


def model_supports_extended_output_beta(model: str) -> bool:
    """Whether ``model`` is eligible for the 300k batch-output beta.

    The extended-output decision must read from the capability
    registry rather than testing ``model in OPUS_MODELS``. Sonnet 4.6
    supports the ``output-300k-2026-03-24`` beta on Message Batches,
    which the family-style check incorrectly excluded.
    """
    return model_capabilities(model).supports_extended_output_beta


# Phase identifiers (declared above so the phase→budget registry can use
# them) gate per-phase request decisions. ``_PHASES_NO_THINKING`` is the
# extension point for phases that should never request thinking regardless
# of model capability — currently only the Haiku triage classifier, which
# is a shallow batch-classification pass.
_PHASES_NO_THINKING: frozenset[str] = frozenset({PHASE_TRIAGE})


def thinking_config_for(*, model: str, phase: str) -> dict | None:
    """Return the ``thinking`` request parameter for ``(model, phase)``.

    Returns ``None`` when the parameter should be omitted entirely —
    either the phase opts out, or the model does not support adaptive
    thinking. Callers should branch on ``is None``; the Anthropic API
    rejects ``thinking=null``.
    """
    if phase in _PHASES_NO_THINKING:
        return None
    if not model_supports_adaptive_thinking(model):
        return None
    return {"type": "adaptive"}


def apply_thinking_config(kwargs: dict, *, model: str, phase: str) -> dict:
    """Insert the ``thinking`` key into ``kwargs`` only when applicable.

    Mutates and returns ``kwargs`` for fluent use. The key is omitted
    entirely (not set to ``None``) when thinking is not applicable, because
    the Anthropic API rejects ``thinking=null``.
    """
    config = thinking_config_for(model=model, phase=phase)
    if config is not None:
        kwargs["thinking"] = config
    return kwargs


# ---------------------------------------------------------------------------
# Output-config effort policy
# ---------------------------------------------------------------------------
#
# The Anthropic API accepts an ``output_config.effort`` parameter on
# supported models. The value tunes how eagerly the model produces tokens
# and how aggressively it pursues tool calls. The documented levels are
# ``low`` / ``medium`` / ``high`` / ``xhigh`` (plus ``max``). The review and
# cross-check phases use ``xhigh`` — Anthropic recommends it as the starting
# point for coding/agentic work on Opus 4.8, and per-spec review is the
# deepest-reasoning phase in the pipeline. We still don't use ``max`` (it
# overshoots without a measured benefit for this workload), and verification
# stays at medium/high so the verdict envelope doesn't balloon.
#
# ``xhigh`` is gated per model: Opus 4.8 and Sonnet 5 accept it; Sonnet
# 4.6's supported set is ``{low, medium, high, max}`` — it rejects ``xhigh``
# at submit with a 400 ("This model does not support effort level 'xhigh'").
# So ``supports_effort`` being a coarse boolean is not enough: a phase that
# defaults to ``xhigh`` but runs on a model without ``supports_xhigh_effort``
# (e.g. cross-check under a pinned Sonnet 4.6, or a review override to an
# older Sonnet) must clamp down to ``high`` or the request fails.
# :func:`effort_config_for` does this clamp via
# :func:`_clamp_effort_for_model`. On the current defaults nothing clamps:
# cross-check / compliance run their declared ``xhigh`` natively on Sonnet 5.
#
# Effort is a request-policy decision, not a prompt one. Centralizing it
# here keeps every request site (review / batch review / cross-check /
# verification / retry / continuation) reaching for the same lever via
# :func:`apply_effort_config`. Unsupported models silently omit the
# parameter via :func:`model_supports_effort`.
#
# Default policy:
#
# - Sonnet verification (PHASE_VERIFICATION{,_RETRY,_CONTINUATION}): medium.
#   (Sonnet 5 at medium is comparable to Sonnet 4.6 at high, so the verdict
#   envelope stays tight while the initial pass got smarter for free.)
# - Opus verification (i.e. escalation): high.
# - Deep review (PHASE_REVIEW, PHASE_CROSS_CHECK, PHASE_COMPLIANCE): xhigh —
#   native on Opus 4.8 and Sonnet 5 alike.
# - Older-Sonnet override (e.g. a pinned Sonnet 4.6): xhigh clamps to high.
# - Triage (Haiku): omit (Haiku does not support effort).
# - Unknown model: omit.

EFFORT_MEDIUM = "medium"
EFFORT_HIGH = "high"
EFFORT_XHIGH = "xhigh"

# Phases whose request paths route through ``output_config.effort``. Triage
# is intentionally omitted — it defaults to Haiku which does not support
# effort, and the workload is a small classification pass that does not
# benefit from elevated effort.
_PHASE_DEFAULT_EFFORT: dict[str, str] = {
    PHASE_REVIEW: EFFORT_XHIGH,
    PHASE_CROSS_CHECK: EFFORT_XHIGH,
    PHASE_VERIFICATION: EFFORT_MEDIUM,
    PHASE_VERIFICATION_RETRY: EFFORT_MEDIUM,
    PHASE_VERIFICATION_CONTINUATION: EFFORT_MEDIUM,
    # Research is retrieval-heavy but not the deepest-reasoning phase;
    # ``high`` keeps the model persistent about chasing primary sources
    # without the xhigh token eagerness the review phases warrant.
    PHASE_RESEARCH: EFFORT_HIGH,
    # Compliance is a deep-evaluation pass like cross-check; ``xhigh``
    # matches. Sonnet 5 (the pass's fixed model) runs it natively;
    # ``_clamp_effort_for_model`` still drops it to ``high`` should the
    # pass ever run on a model without ``supports_xhigh_effort``.
    PHASE_COMPLIANCE: EFFORT_XHIGH,
    # The drawing digest reads and transcribes documents it was handed —
    # no tools to chase, no deep reasoning; ``medium`` keeps the output
    # disciplined against the per-chunk length contract.
    PHASE_DRAWING_DIGEST: EFFORT_MEDIUM,
    # Drawing-impact synthesis reasons about how the digest relates to the
    # findings — a genuine (if bounded) reasoning task, but lighter than the
    # deep review phases; ``high`` keeps it grounded without the xhigh token
    # eagerness. Clamps to ``high`` anyway on any non-xhigh model.
    PHASE_DRAWING_IMPACT: EFFORT_HIGH,
}

# Verification phases get the model-aware bump: Opus on verification is
# always the escalation tier, so the policy lifts effort to ``high``.
_VERIFICATION_PHASES: frozenset[str] = frozenset(
    {
        PHASE_VERIFICATION,
        PHASE_VERIFICATION_RETRY,
        PHASE_VERIFICATION_CONTINUATION,
    }
)

# Effort levels only ``supports_xhigh_effort`` models accept (Opus 4.8,
# Sonnet 5). Sonnet 4.6's supported set is ``{low, medium, high, max}``; it
# rejects ``xhigh`` at submit with a 400 ("This model does not support effort
# level 'xhigh'"). Membership in this set is the trigger for
# :func:`_clamp_effort_for_model` to downgrade to ``high`` on a model whose
# capability entry lacks the flag. Adding a future gated level here makes
# every phase clamp it automatically on non-supporting models.
_XHIGH_GATED_EFFORT_LEVELS: frozenset[str] = frozenset({EFFORT_XHIGH})


def _clamp_effort_for_model(level: str, model: str) -> str:
    """Clamp an effort ``level`` down to what ``model`` accepts.

    ``xhigh`` requires the capability whitelist's ``supports_xhigh_effort``
    flag (Opus 4.8, Sonnet 5); on any other model it falls back to ``high``
    — the deepest level Sonnet 4.6 accepts (we don't use ``max``). Every
    other level passes through unchanged. This is what keeps an ``xhigh``
    phase (cross-check / compliance / review) from 400-ing at submit when an
    env override pins a model without the flag; unknown ids clamp too, since
    the conservative default capabilities leave the flag off.
    """
    if (
        level in _XHIGH_GATED_EFFORT_LEVELS
        and not model_capabilities(model).supports_xhigh_effort
    ):
        return EFFORT_HIGH
    return level


def effort_config_for(*, model: str, phase: str) -> dict | None:
    """Return the ``output_config`` dict for ``(model, phase)``, or ``None``.

    Returns ``None`` (i.e. "omit the field") when:

    - the model does not support effort (Haiku, unknown / future models),
    - the phase has no registered default (triage — defaults to Haiku,
      which already short-circuits above).

    Otherwise returns ``{"effort": <level>}`` where the level is ``high``
    for Opus on a verification phase (the escalation tier) or the phase
    default from :data:`_PHASE_DEFAULT_EFFORT`, clamped to what ``model``
    supports (``xhigh`` → ``high`` on non-Opus models — see
    :func:`_clamp_effort_for_model`).
    """
    if not model_supports_effort(model):
        return None

    if phase in _VERIFICATION_PHASES:
        # Opus on a verification phase is the escalation tier — every
        # initial verification call routes to Sonnet by default.
        if model in OPUS_MODELS:
            return {"effort": EFFORT_HIGH}
        return {"effort": EFFORT_MEDIUM}

    level = _PHASE_DEFAULT_EFFORT.get(phase)
    if level is None:
        return None
    return {"effort": _clamp_effort_for_model(level, model)}


def apply_effort_config(kwargs: dict, *, model: str, phase: str) -> dict:
    """Insert ``output_config`` into ``kwargs`` only when applicable.

    Mutates and returns ``kwargs`` for fluent use. The key is omitted
    entirely (not set to ``None``) when effort is not applicable, because
    the Anthropic API rejects ``output_config=null``.

    Mirrors :func:`apply_thinking_config` so request builders pair the
    two helpers the same way per directive 4 ("Pair effort decisions
    with thinking decisions where appropriate").
    """
    config = effort_config_for(model=model, phase=phase)
    if config is not None:
        kwargs["output_config"] = config
    return kwargs


# ---------------------------------------------------------------------------
# Prompt caching (centralized phase-aware policy)
# ---------------------------------------------------------------------------
#
# Each phase declares whether its system prompt and tool list are stable /
# large / repeated enough to benefit from caching. Caching is enabled for
# high-value phases (review, batch review, cross-check, verification +
# retry/continuation) and disabled for triage where the prompt is below
# the Anthropic cache minimum (2048 tokens for Haiku) so a cache write
# would be paid for nothing.


@dataclass(frozen=True)
class CachePolicy:
    """Per-phase cache policy.

    ``cache_system`` and ``cache_tools`` independently control whether the
    system prompt and the trailing tool block carry ``cache_control``
    breakpoints.
    """

    cache_system: bool
    cache_tools: bool

    @property
    def caches_anything(self) -> bool:
        return self.cache_system or self.cache_tools


_DEFAULT_PHASE_CACHE_POLICY = CachePolicy(cache_system=True, cache_tools=True)

_PHASE_CACHE_POLICY: dict[str, CachePolicy] = {
    PHASE_REVIEW: CachePolicy(cache_system=True, cache_tools=True),
    PHASE_CROSS_CHECK: CachePolicy(cache_system=True, cache_tools=True),
    PHASE_VERIFICATION: CachePolicy(cache_system=True, cache_tools=True),
    PHASE_VERIFICATION_RETRY: CachePolicy(cache_system=True, cache_tools=True),
    PHASE_VERIFICATION_CONTINUATION: CachePolicy(cache_system=True, cache_tools=True),
    # Research: the system prompt (persona + protocol) and tool list are
    # shared across every dimension call and every pause_turn resume in a
    # run, so both breakpoints pay for themselves on the second call.
    PHASE_RESEARCH: CachePolicy(cache_system=True, cache_tools=True),
    # Compliance: one call on small projects, several on chunked ones —
    # the stable system prompt + tool block pay back on chunk #2 and on
    # retries, mirroring cross-check.
    PHASE_COMPLIANCE: CachePolicy(cache_system=True, cache_tools=True),
    # Triage: ~375-token system prompt called in batches of up to 20,
    # below the 2048-token Haiku cache minimum so repeated calls cannot
    # hit. Skip caching to avoid the cache-write cost.
    PHASE_TRIAGE: CachePolicy(cache_system=False, cache_tools=False),
    # Drawing digest: the system prompt (protocol/format contract) is
    # byte-identical across every chunk and retry in a run, so the
    # breakpoint pays back on chunk #2. The phase sends no tools at all;
    # ``cache_tools=False`` documents that (``tools_with_cache`` already
    # no-ops on an empty list).
    PHASE_DRAWING_DIGEST: CachePolicy(cache_system=True, cache_tools=False),
    # Drawing impact: one call per run (not chunked), but the stable
    # system prompt + tool block pay back on a retry — mirror cross-check
    # / compliance rather than the tool-less digest.
    PHASE_DRAWING_IMPACT: CachePolicy(cache_system=True, cache_tools=True),
}


def cache_policy_for(phase: str | None) -> CachePolicy:
    """Return the per-phase :class:`CachePolicy`.

    Unknown phases fall back to the conservative default (cache both
    system prompt and tools).
    """
    if phase is None:
        return _DEFAULT_PHASE_CACHE_POLICY
    return _PHASE_CACHE_POLICY.get(phase, _DEFAULT_PHASE_CACHE_POLICY)


def _cache_control_block() -> dict:
    """Return the standard 1-hour ephemeral cache_control block.

    Spec Critic batch + verification waves run for 30 minutes to several
    hours, well beyond the 5-minute default ephemeral cache TTL. The
    1-hour TTL costs 2x the cache write but typically pays back inside
    the second wave of a batch verification cycle, where the same system
    prompt is sent hundreds of times.
    """
    return {"type": "ephemeral", "ttl": "1h"}


def system_prompt_with_cache(prompt: str, *, phase: str | None = None):
    """Return a system payload with a cache breakpoint when policy permits.

    Per the Anthropic prompt-caching docs, including the same cache_control
    blocks in every request in a batch lets later items hit the cache
    created by earlier items.
    """
    policy = cache_policy_for(phase)
    if not policy.cache_system:
        return prompt
    return [
        {
            "type": "text",
            "text": prompt,
            "cache_control": _cache_control_block(),
        }
    ]


def tools_with_cache(tools: list[dict], *, phase: str | None = None) -> list[dict]:
    """Attach a cache breakpoint to the last tool definition.

    Tool schemas are stable across verification calls. Caching the trailing
    tool block lets the rest of the request (system prompt + tool defs)
    share one cache prefix. The system prompt has its own breakpoint via
    :func:`system_prompt_with_cache`, so changing only a tool definition
    invalidates only the tools-level cache entry.

    ``phase`` selects the per-phase policy. When the policy disables tool
    caching for the phase (e.g. triage where the prompt is below the cache
    minimum), the tool list is returned unchanged.
    """
    if not tools:
        return tools
    policy = cache_policy_for(phase)
    if not policy.cache_tools:
        return tools
    last = dict(tools[-1])
    last["cache_control"] = _cache_control_block()
    return [*tools[:-1], last]


# ---------------------------------------------------------------------------
# Service tier (priority capacity)
# ---------------------------------------------------------------------------


def batch_service_tier() -> str:
    """Return the ``service_tier`` parameter for batch request params.

    ``auto`` opts batch requests into priority capacity when available,
    falling back to standard.
    """
    return "auto"


# ---------------------------------------------------------------------------
# Anthropic token-counting preflight
# ---------------------------------------------------------------------------

def token_count_preflight_enabled() -> bool:
    """Whether to call Anthropic's count_tokens endpoint before submission.

    Always True. The GUI also runs an exact count for the largest spec
    when the file list changes; the pipeline call here is the moment-of-
    truth guard before a real submission.
    """
    return True


# ---------------------------------------------------------------------------
# Web-search tool configuration
# ---------------------------------------------------------------------------

# Source-quality blocklist for ``web_search_20260209``. Mixing
# ``allowed_domains`` and ``blocked_domains`` is not supported by the tool,
# so this is blocked-only; California priority sources are documented in the
# verifier system prompt as guidance rather than encoded as an allow-list.
#
# Domains are listed bare (no scheme/path) and the tool treats each entry as
# "this apex and every subdomain", so adding ``simple.wikipedia.org`` when
# ``wikipedia.org`` is already on the list adds nothing.
#
# Categories (kept as inline comment groups so the intent of each line is
# obvious; do *not* hand-sort across categories without checking that no
# entry's category interpretation changes):
#   - Aggregators / Q&A: forums where contractor-grade evidence is rare.
#   - LLM-assistant outputs: another model's answer is not a citable source.
#   - Trade forums: useful peer chatter, not authoritative for code.
#   - DIY / home-improvement content farms.
#   - Social: unsuitable for a defensible engineering review.
#   - General encyclopedias: tertiary sources.
#
# TODO: explore a category-based blocking helper so each entry is annotated
# with its category and the report can explain *why* a citation was rejected
# (deferred for now; the immediate fix here is just deduplicating the
# obvious subdomain overlap). Any change to this list should be exercised
# against the verifier's grounding tests in
# ``tests/test_source_grounding_invariant.py``.
_WEB_SEARCH_BLOCKED_DOMAINS = [
    # Aggregators / Q&A
    "reddit.com", "quora.com", "medium.com",
    "stackexchange.com", "stackoverflow.com",
    "answers.yahoo.com", "fixya.com",
    # LLM-assistant outputs
    "chatgpt.com", "perplexity.ai", "openai.com", "gemini.google.com",
    "claude.ai", "you.com", "phind.com", "copilot.microsoft.com",
    "poe.com", "character.ai", "jasper.ai", "writesonic.com",
    # Trade forums (peer chatter, not authoritative for code compliance)
    "diychatroom.com", "forums.jlconline.com", "hvac-talk.com",
    "inspectionnews.net", "inspectorsforum.com", "contractortalk.com",
    # DIY / home-improvement / lead-gen content farms
    "doityourself.com", "homeadvisor.com", "thumbtack.com", "angi.com",
    "ehow.com", "wikihow.com", "about.com", "thespruce.com", "bobvila.com",
    "familyhandyman.com", "hunker.com", "sapling.com", "reference.com",
    "leaf.tv", "sciencing.com", "bizfluent.com", "pocketsense.com",
    # Social
    "facebook.com", "twitter.com", "x.com", "instagram.com", "tiktok.com",
    "linkedin.com", "pinterest.com", "youtube.com", "threads.net",
    # General encyclopedias (tertiary). ``wikipedia.org`` already covers
    # every subdomain (``simple.wikipedia.org``, ``en.wikipedia.org``, ...).
    "wikipedia.org", "britannica.com",
]

# Fallback budget for severities outside the known set.
DEFAULT_VERIFICATION_MAX_USES = 5

# Per-severity search budgets. High-stakes claims get more rope; editorial
# gripes get less. Applied identically to real-time and batch verification
# paths so the budget shape doesn't depend on which mode you ran in.
_SEVERITY_MAX_USES: dict[str, int] = {
    "CRITICAL": 8,
    "HIGH": 7,
    "MEDIUM": 5,
    "GRIPES": 3,
}


def web_search_max_uses_for_severity(severity: str | None) -> int:
    """Return the per-severity web_search budget.

    Falls back to ``DEFAULT_VERIFICATION_MAX_USES`` for unknown severities so
    a misclassified finding still gets a reasonable budget.
    """
    sev = (severity or "").strip().upper()
    return _SEVERITY_MAX_USES.get(sev, DEFAULT_VERIFICATION_MAX_USES)


# Per-run web-search research budgets (requirements-research fan-out).
# These are the ENGINE defaults; a module's ``ResearchDimension`` may
# override per dimension (0 ⇒ fall back to these). Re-baselined from field
# measurement (hyperscale DC plan D-11 [FT]): the heavy governing-codes /
# AHJ dimensions need 20–24 searches to reach referenced-standards-table
# depth, so modules are expected to raise these for those dimensions.
RESEARCH_DEFAULT_MAX_SEARCHES = 12
RESEARCH_DEFAULT_MAX_FETCHES = 4


def build_web_search_tool(
    *,
    max_uses: int = DEFAULT_VERIFICATION_MAX_USES,
    user_location: dict | None = None,
) -> dict:
    """Build the web_search server-tool dict.

    ``user_location`` steers search localization. ``None`` (every existing
    call site) keeps the long-standing hardcoded California default
    byte-identical — the CA module's request shape must not change. A run
    with a :class:`~src.core.project_profile.ProjectProfile` passes
    ``profile.web_search_user_location()`` so research (WS-3) and
    verification (WS-4) search as the project's own locale.
    """
    return {
        "type": "web_search_20260209",
        "name": "web_search",
        "blocked_domains": list(_WEB_SEARCH_BLOCKED_DOMAINS),
        "max_uses": max_uses,
        "user_location": dict(user_location) if user_location else {
            "type": "approximate",
            "country": "US",
            "region": "California",
        },
    }


# ---------------------------------------------------------------------------
# Web-fetch tool configuration
# ---------------------------------------------------------------------------
#
# The ``web_fetch_20260209`` server tool is the companion to ``web_search``:
# it pulls the full text of a previously-seen URL (URLs are required to have
# appeared in a prior web_search result block in the same conversation
# context, so the model cannot fetch arbitrary URLs it invented). Per
# Anthropic's pricing docs, web_fetch carries no per-request surcharge —
# the caller pays only for the tokens the fetched content consumes — so
# the safety knob here is ``max_uses`` plus ``max_content_tokens``, not a
# billing rate.
#
# Used by STANDARD_REASONING and DEEP_REASONING verification modes only;
# STRICT_STRUCTURED / LOCAL_SKIP intentionally omit the tool because those
# modes are explicitly cheap/narrow and don't benefit from a deep dive into
# a single source page.

# Per-request fetch budget. Lower than the search budget by design — a
# verification call typically needs at most one or two full-page fetches
# to confirm a borderline claim; more than that is a sign the model is
# spinning rather than converging.
DEFAULT_VERIFICATION_MAX_FETCHES = 3

# Truncation ceiling on fetched-page content. Large code-publisher pages
# (up.codes / iccsafe.org / nfpa.org) can easily exceed 100k tokens of
# rendered text; we cap at 50k so a single fetch cannot blow the
# verification input window. The model gets enough context to find the
# clause it cares about without forcing the verifier to truncate the
# response.
WEB_FETCH_MAX_CONTENT_TOKENS = 50_000


def build_web_fetch_tool(*, max_uses: int = DEFAULT_VERIFICATION_MAX_FETCHES) -> dict:
    """Build the web_fetch server-tool dict for a verification request.

    Tool type pinned to ``web_fetch_20260209`` per Anthropic's web-fetch
    server-tool spec. Web fetch is generally available and needs no
    ``anthropic-beta`` header — the tool dict alone enables it, and sending a
    (retired) beta value such as ``web-fetch-2026-02-09`` is rejected with
    HTTP 400 ``invalid_request_error``.

    The ``citations`` field is enabled so cited URLs land in the
    assistant message's source-grounding partition the same way web_search
    citations do; ``max_content_tokens`` caps the truncation length so
    one fetch on a giant code-publisher page cannot dominate the verifier
    response window. ``blocked_domains`` mirrors the web_search blocklist
    so the two tools share one source-quality policy — a domain we won't
    search is a domain we won't fetch either.
    """
    return {
        "type": "web_fetch_20260209",
        "name": "web_fetch",
        "blocked_domains": list(_WEB_SEARCH_BLOCKED_DOMAINS),
        "max_uses": max_uses,
        "citations": {"enabled": True},
        "max_content_tokens": WEB_FETCH_MAX_CONTENT_TOKENS,
    }


# Web fetch is generally available and takes NO ``anthropic-beta`` header —
# the tool dict above is sufficient to enable it. The verification request
# builder therefore attaches no beta header for web_fetch. Sending a retired
# beta value such as ``web-fetch-2026-02-09`` is rejected by the API with
# HTTP 400 ``invalid_request_error: Unexpected value(s) ... for the
# anthropic-beta header`` — an unrecognized beta value is not silently
# ignored, so it must not be sent at all.


# ---------------------------------------------------------------------------
# Cache-token usage extraction (for diagnostics)
# ---------------------------------------------------------------------------

def extract_cache_usage(usage) -> dict[str, int]:
    """Pull cache-related fields off an Anthropic usage object.

    Returns a dict with keys ``cache_creation_input_tokens`` and
    ``cache_read_input_tokens`` (zero when absent). The Anthropic SDK
    exposes these on ``Message.usage`` when prompt caching is in effect.
    """
    if usage is None:
        return {"cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    return {
        "cache_creation_input_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        "cache_read_input_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
    }


# ---------------------------------------------------------------------------
# Cache diagnostics (beta, opt-in observability)
# ---------------------------------------------------------------------------
#
# The ``cache-diagnosis-2026-04-07`` beta lets a request carry a
# ``diagnostics.previous_message_id`` and receive a ``diagnostics`` object on
# the response that fingerprints the current and previous request and reports
# the first point of prompt-prefix divergence — i.e. *why* a cache hit did not
# occur. It is a debugging aid for the cache-breakpoint-stability invariant
# this app cares about, NOT a request-shape change, so it stays default-off and
# is requested only when an operator is actively investigating a miss.
#
# Constraints worth remembering at the call site:
#   - First-party Claude API only (unavailable on Bedrock / Vertex).
#   - Needs a *previous* message id to diff against, so it produces signal only
#     on sequential same-prefix synchronous calls (the verification
#     continuation loop), never on the Batch API (batch items have no prior
#     message id to reference).

ENV_CACHE_DIAGNOSTICS = "SPEC_CRITIC_CACHE_DIAGNOSTICS"
CACHE_DIAGNOSTICS_BETA = "cache-diagnosis-2026-04-07"

# Mirrors the disable-token convention used by the tracing / cache modules.
_DISABLE_TOKENS = frozenset({"0", "false", "no", "off"})


def cache_diagnostics_enabled() -> bool:
    """Whether to request prompt-cache diagnostics. Default OFF.

    Opt-in via ``SPEC_CRITIC_CACHE_DIAGNOSTICS`` set to any truthy,
    non-disable value. Off by default because it is a beta, first-party-only
    observability feature that only an operator chasing a cache miss needs;
    leaving it off keeps the request byte-identical to today.
    """
    raw = os.environ.get(ENV_CACHE_DIAGNOSTICS)
    if raw is None:
        return False
    val = raw.strip().lower()
    return val != "" and val not in _DISABLE_TOKENS


def cache_diagnostics_params(
    previous_message_id: str | None,
) -> tuple[dict | None, dict | None]:
    """Return ``(extra_body, extra_headers)`` to request cache diagnostics.

    Returns ``(None, None)`` unless cache diagnostics is enabled AND a
    ``previous_message_id`` is supplied — the feature is meaningless without a
    prior message to diff against, so an isolated call cleanly no-ops.

    The body param rides the SDK ``extra_body`` seam and the beta rides
    ``extra_headers`` (``anthropic-beta``) so this stays correct on SDK
    versions that do not yet model ``diagnostics`` natively — the same
    transport-seam discipline the verification request builder already uses.
    """
    if not previous_message_id or not cache_diagnostics_enabled():
        return None, None
    extra_body = {"diagnostics": {"previous_message_id": previous_message_id}}
    extra_headers = {"anthropic-beta": CACHE_DIAGNOSTICS_BETA}
    return extra_body, extra_headers


def extract_cache_diagnostics(message) -> dict | None:
    """Pull the beta ``diagnostics`` object off a response message, if present.

    Defensive by construction: the SDK ``Message`` model is configured
    ``extra="allow"``, so an unmodeled ``diagnostics`` field round-trips as an
    attribute. Returns ``None`` when absent (the common case, or the feature
    disabled) or on any access/serialization error — a diagnostics read must
    never sink a verification.
    """
    try:
        diag = getattr(message, "diagnostics", None)
    except Exception:
        return None
    if diag is None:
        return None
    if isinstance(diag, dict):
        return diag
    dumper = getattr(diag, "model_dump", None)
    if callable(dumper):
        try:
            return dumper()
        except Exception:
            return None
    return None
