"""Centralized Anthropic API configuration for Spec Critic.

Single place for model identifiers, per-phase output-token caps, batch
beta headers, web-search tool configuration, and request-shape policy
(prompt caching, adaptive thinking, effort).

Model identifiers may be overridden via env vars:
    SPEC_CRITIC_REVIEW_MODEL                — review (default Opus 5.5).
    SPEC_CRITIC_VERIFICATION_MODEL          — verification initial pass
                                              (default Sonnet 5.5).
    SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL — escalation (default Opus 5.5).
    SPEC_CRITIC_TRIAGE_MODEL                — verification triage
                                              (default Haiku 4.5).
    SPEC_CRITIC_RESEARCH_MODEL              — requirements research fan-out
                                              (default Sonnet 5.5).
    SPEC_CRITIC_DRAWING_DIGEST_MODEL        — construction-drawing digest
                                              vision pass (default Sonnet 5.5).
    SPEC_CRITIC_DRAWING_IMPACT_MODEL        — drawing-impact synthesis
                                              (default Sonnet 5.5).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model identifiers (centralized)
# ---------------------------------------------------------------------------

MODEL_OPUS_55 = "claude-opus-5-5"
# Previous-generation Opus models. Kept registered (constant + capability
# entry) for the same reason as Sonnet 4.6 below: a pinned env override must
# still build a correct request shape.
MODEL_OPUS_5 = "claude-opus-5"
MODEL_OPUS_48 = "claude-opus-4-8"
MODEL_SONNET_55 = "claude-sonnet-5-5"
# Previous-generation Sonnet models. Kept registered (constant + capability
# entry) so operator env overrides that pin them keep their correct request
# shape — most notably the ``xhigh`` → ``high`` effort clamp, which 4.6 needs
# and the Sonnet 5 generation does not.
MODEL_SONNET_5 = "claude-sonnet-5"
MODEL_SONNET_46 = "claude-sonnet-4-6"
MODEL_HAIKU_45 = "claude-haiku-4-5"

# Review runs on the current Opus flagship; verification routes through
# Sonnet first and reserves Opus for escalation on CRITICAL/HIGH UNVERIFIED
# findings. Defaults track the newest generation of each tier (Opus 5.5 /
# Sonnet 5.5). Override any of these via the matching ``SPEC_CRITIC_*_MODEL``
# env var.
REVIEW_MODEL_DEFAULT = os.environ.get("SPEC_CRITIC_REVIEW_MODEL", MODEL_OPUS_55)
CROSS_CHECK_MODEL_DEFAULT = MODEL_SONNET_55
VERIFICATION_MODEL_DEFAULT = os.environ.get(
    "SPEC_CRITIC_VERIFICATION_MODEL", MODEL_SONNET_55
)

# Model used when escalating a low-confidence/high-severity verification.
VERIFICATION_ESCALATION_MODEL = os.environ.get(
    "SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL", MODEL_OPUS_55
)

# Verification triage pre-pass (triage.classify_findings_with_haiku) decides
# whether a finding can be locally resolved or needs web verification. The
# task is shallow classification over short inputs; Haiku fits.
TRIAGE_MODEL_DEFAULT = os.environ.get("SPEC_CRITIC_TRIAGE_MODEL", MODEL_HAIKU_45)

# Requirements-research fan-out (per-dimension web_search calls that build
# the Project Requirements Profile for profile-enabled modules). Sonnet:
# the task is retrieval + structured summarization, not deep review.
RESEARCH_MODEL_DEFAULT = os.environ.get("SPEC_CRITIC_RESEARCH_MODEL", MODEL_SONNET_55)

# Local-code compliance pass (profile-enabled modules; modeled on
# cross-check). Bound directly to Sonnet with NO env override — deliberate
# parity with ``CROSS_CHECK_MODEL_DEFAULT``, which is likewise unswappable.
COMPLIANCE_MODEL_DEFAULT = MODEL_SONNET_55

# Construction-drawing digest (one-time vision pass at attach time that
# turns drawing PDFs into a plain-text Project Context block). Sonnet: the
# task is transcription + structured summarization of provided documents,
# not deep review. Every whitelisted model accepts PDF document blocks
# (there is no ``supports_vision`` capability flag — a non-vision override
# fails fast at the digest call itself, before anything downstream is
# billed).
DRAWING_DIGEST_MODEL_DEFAULT = os.environ.get(
    "SPEC_CRITIC_DRAWING_DIGEST_MODEL", MODEL_SONNET_55
)

# Drawing-impact synthesis (one post-review pass that explains, for the
# report, how the attached construction drawings informed the review — it
# cross-references the final findings against the drawing digest already in
# Project Context). Sonnet: the task is grounded synthesis over text the run
# already produced, not deep review. Env-overridable like the digest it
# reads, defaulting to the same tier.
DRAWING_IMPACT_MODEL_DEFAULT = os.environ.get(
    "SPEC_CRITIC_DRAWING_IMPACT_MODEL", MODEL_SONNET_55
)


# Opus family membership now drives exactly one policy decision: the Opus
# effort ceiling (every Opus request runs at ``OPUS_EFFORT_CEILING`` —
# ``medium`` — or below; see :func:`effort_config_for`). Output ceilings
# resolve through the capability whitelist
# (``model_capabilities(model).max_output_tokens``), and the ``xhigh`` effort
# gate is the per-model ``supports_xhigh_effort`` flag — neither depends on
# this set, so a new Opus id missing from it can never be silently clamped to
# a smaller output cap; it would only escape the effort ceiling.
OPUS_MODELS = frozenset({MODEL_OPUS_55, MODEL_OPUS_5, MODEL_OPUS_48})

# Models whose vision tier is the high-resolution one (2576px long edge,
# ~4784-token image cap). Sonnet 5 was the first Sonnet-tier model with
# high-res image support, so this can't be OPUS_MODELS anymore. Consumed by
# ``tokenizer._image_caps_for_model`` for image-token cost estimates, where
# the larger cap is also the conservative one.
HIRES_VISION_MODELS = frozenset(
    {MODEL_OPUS_55, MODEL_OPUS_5, MODEL_OPUS_48, MODEL_SONNET_55, MODEL_SONNET_5}
)


# ---------------------------------------------------------------------------
# Output-token caps
# ---------------------------------------------------------------------------

# Hard ceilings imposed by the model.
MAX_OUTPUT_TOKENS_OPUS = 128_000
MAX_OUTPUT_TOKENS_SONNET_5 = 128_000  # Sonnet 5 / 5.5 match the Opus ceiling
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
# The verdict itself is short, but thinking counts toward ``max_tokens`` even
# when it is not returned, and a verification turn thinks between several
# searches (Anthropic's Opus 5.5 and Sonnet 5.5 prompting guides: size
# ``max_tokens`` for the thinking plus the reply; Opus 5.5 thinks more per
# turn than Opus 5 at the same effort). A ``max_tokens`` stop fails the
# verification after its searches were paid for, and a higher cap costs
# nothing unless it is used: output is billed as generated, the model does not
# see ``max_tokens``, and it does not count against the output-tokens-per-
# minute rate limit (Anthropic's rate-limits page). Every verification call
# streams or runs in a batch, so the SDK's non-streaming size guard does not
# apply. Was 16k.
VERIFICATION_OUTPUT_CAP = 64_000
# Triage emits a small array of {index, classification, reason}; 8k is more
# than enough even for a 50-finding chunk.
HAIKU_TRIAGE_OUTPUT_CAP = 8_000
# One research dimension returns a structured item list plus tool-use /
# thinking overhead. Field measurement (hyperscale DC plan, D-11 [FT]):
# dimension outputs ran 6–14k tokens before protocol overhead, so the
# original 16k-style verification cap would truncate the heavy dimensions.
# Raised from 24k for the thinking between up to 24 searches, on the same
# grounds as the verification cap above (free unless used; streamed).
RESEARCH_OUTPUT_CAP = 64_000
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
# output is naturally small; the cap leaves room for the thinking in front of
# it at ``high`` effort (see the verification cap above). Was 16k.
DRAWING_IMPACT_OUTPUT_CAP = 32_000

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
# Verification retry/continuation reuse the verification cap — the verdict
# envelope is unchanged across retries. (An earlier comment here said a larger
# cap "only invites the model to ramble"; the model does not see
# ``max_tokens``, so the cap changes only where a reply is cut off.)
# The cap a phase missing from the registry below gets: small on purpose, so
# forgetting to register a phase is noticed as truncation rather than hidden.
UNREGISTERED_PHASE_OUTPUT_CAP = 16_000

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
    and triage all share one registry. Unknown phases fall back to
    :data:`UNREGISTERED_PHASE_OUTPUT_CAP` (16k) — a future phase that forgets
    to register loses headroom instead of accidentally inheriting the 128k
    review cap. (The fallback was the verification cap until that cap was
    raised for thinking headroom; it keeps the old 16k value.)
    """
    requested = _PHASE_OUTPUT_BUDGET.get(phase, UNREGISTERED_PHASE_OUTPUT_CAP)
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
# synchronous calls (up to the 128k phase cap of output — 5-8x the output
# budget of any other streaming phase), so the default pool is
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
    # Whether the model accepts ``output_config.effort: "xhigh"``. Opus 5.5,
    # Opus 5, Opus 4.8, Sonnet 5.5 and Sonnet 5 do; Sonnet 4.6's supported
    # set is {low, medium, high, max} and it rejects ``xhigh`` at submit with
    # a 400. Consulted by
    # ``_clamp_effort_for_model`` — a phase that defaults to ``xhigh`` on a
    # model without this flag clamps down to ``high`` instead of erroring.
    # Default ``False`` so unknown models take the safe clamp.
    supports_xhigh_effort: bool = False
    # Whether the model can be sent the ``web_fetch`` server tool. This is NOT
    # uniform across current models: Anthropic's Opus 5 migration guide states
    # that Claude Opus 5 supports the same feature set as Opus 4.8 "with two
    # exceptions: web fetch is not available on Claude Opus 5, and Priority
    # Tier is not supported" — and the web-fetch tool page's supported-model
    # list names Fable 5 / Opus 4.8 / Mythos 5 / Opus 4.7 / Opus 4.6 /
    # Sonnet 5 / Sonnet 4.6 while conspicuously omitting Opus 5.
    #
    # Consulted by ``verification_routing.build_verification_tools_from_decision``,
    # which would otherwise attach ``web_fetch_20260209`` to every
    # STANDARD_REASONING / DEEP_REASONING verification — including the Opus
    # escalation tier, the app's highest-stakes path. Default ``False`` so an
    # unknown override omits the tool (a smaller request) rather than risking
    # a rejection, matching every other optional capability here.
    #
    # Opus 5.5 is left ``False`` too (rechecked 2026-09-29): its migration
    # guide says it keeps Opus 5's feature set and "the same server-side and
    # client-side tools", and says nothing about web fetch. The web-fetch
    # page no longer lists supported models (it defers to the tool reference,
    # which defers back to each tool's page), though its code samples now use
    # ``claude-opus-5-5`` — suggestive, not a statement of support. Turning
    # it on is a deliberate change after the live probe
    # (``tests/test_network_smoke.py::test_opus_5_5_web_fetch_probe_smoke``),
    # since a wrong ``True`` would 400 every deep-reasoning escalation.
    #
    # (The Priority Tier exception needs no flag: the app only ever sends
    # ``service_tier: "auto"``, documented as "uses the Priority Tier capacity
    # if available, falling back to your other capacity if not" — a
    # non-eligible model degrades to standard rather than erroring. Opus 5.5
    # and Sonnet 5.5 do not support Priority Tier either.)
    supports_web_fetch: bool = False
    # Whether a request that OMITS the ``thinking`` key may carry a forcing
    # ``tool_choice`` (``{"type": "tool", "name": ...}``) on this model, as
    # the triage phase sends it. ``True`` only where that shape has run in
    # production: Haiku 4.5, which never thinks, so an omitted key means no
    # thinking and forced tool use is a plain request. Consulted only by
    # ``structured_schemas.triage_tool_choice``: triage is the one phase in
    # ``_PHASES_NO_THINKING``. Opus 5 and Sonnet 5 run adaptive thinking
    # when the key is omitted; Anthropic's thinking page (rechecked
    # 2026-09-29, plan EX-02) says forced tool use works with adaptive
    # thinking on them, and fails only with manual ``budget_tokens`` thinking
    # and on Opus 5.5 / Sonnet 5.5 / Fable 5.1 / Mythos 5.1, which reject it
    # outright. That is documented, not verified against the live API, so
    # the flag stays ``False`` for them, and for Opus 4.8 and Sonnet 4.6: a
    # ``SPEC_CRITIC_TRIAGE_MODEL`` override keeps today's ``auto`` shape, and
    # widening it is a deliberate, re-pinned decision. Default ``False`` so
    # unknown ids keep ``auto`` (a request the API always accepts).
    supports_forced_tool_choice: bool = False
    # Whether the provider documents forced tool use (``tool_choice``
    # ``{"type": "tool", ...}``) as accepted on a request that carries
    # adaptive thinking (plan EX-02). Anthropic's thinking page, rechecked
    # 2026-09-29: forced tool use "is incompatible with manual extended
    # thinking but works with adaptive thinking", except on Opus 5.5, Sonnet
    # 5.5, Fable 5.1, and Mythos 5.1, which reject it on every request.
    # **Documented, not verified live**: no request of this shape has been
    # sent from this repository. Consulted only by the default-off review
    # output-constraint experiment (``structured_schemas.review_output_mode``);
    # nothing that runs by default reads it. ``False`` for Haiku 4.5, which
    # has no adaptive thinking, and for unknown ids.
    supports_forced_tool_with_thinking: bool = False
    # Whether the provider documents JSON outputs (``output_config.format``
    # with a ``json_schema``) for this model (plan EX-02). Anthropic's
    # structured-outputs page lists Opus 5, Opus 4.8, Sonnet 5, Sonnet 4.6,
    # and Haiku 4.5, and the Opus 5.5 and Sonnet 5.5 migration guides list
    # structured outputs among what carries over. The same page's compatibility table says JSON outputs
    # "cannot be used with extended thinking", while its thinking page tells
    # Opus 5.5 users, whose thinking cannot be turned off, to use structured
    # outputs instead of forced tool use; the two statements disagree, and
    # every review request carries adaptive thinking, so the combination is
    # **unverified** until a live probe sends it
    # (``tests/test_network_smoke.py``). Consulted only by the default-off
    # review output-constraint experiment; ``False`` for unknown ids.
    supports_json_output_format: bool = False
    # Whether the model accepts ``thinking.display`` ("summarized" /
    # "omitted"). The field arrived with Opus 4.7, and on Opus 5.5, Opus 5,
    # Opus 4.8, Sonnet 5.5 and Sonnet 5 the default is "omitted": thinking
    # blocks come back with empty text. Consulted only by :func:`thinking_config_for`, which asks
    # for "summarized" while a deep trace is recording (plan WP-13). Sonnet
    # 4.6 is left ``False``: its default is already "summarized", so a deep
    # trace reads its thinking without the field, and its request stays
    # unchanged. Default ``False`` so an unknown id never gets the field.
    supports_thinking_display: bool = False


_MODEL_CAPABILITIES: dict[str, ModelCapabilities] = {
    MODEL_OPUS_55: ModelCapabilities(
        # Claude Opus 5.5 per Anthropic's models overview and the Opus 5 →
        # Opus 5.5 migration guide (checked 2026-09-29): 1M-token context
        # window, 128k max output, the ``output-300k-2026-03-24`` batch beta
        # (listed among what carries over from Opus 5), the same tokenizer as
        # Opus 5, adaptive thinking, all five effort levels including
        # ``xhigh``, structured outputs, and strict tool use.
        #
        # Its breaking changes, and why none reaches this app today:
        # (1) thinking cannot be disabled — ``{"type": "disabled"}`` and
        #     ``budget_tokens`` 400 at every effort. This codebase never sends
        #     either; ``thinking_config_for`` sends ``adaptive`` or omits the
        #     key, and on this model an omitted key is adaptive thinking.
        # (2) forced ``tool_choice`` (``any`` / ``tool``) 400s on every
        #     request, batch and ``count_tokens`` included. Every Opus-routed
        #     phase sends ``auto``; the only forced shapes are Haiku triage
        #     and the default-off EX-02 arm, and both are gated on the two
        #     flags below, left ``False`` here.
        # (3) the API default effort is ``medium`` (Opus 5's is ``high``).
        #     The app always sends effort explicitly (``effort_config_for``).
        # (4) computer use needs the toolset; the app sends no computer tool.
        # ``supports_web_fetch=False`` follows Opus 5 — see the flag's comment.
        supports_adaptive_thinking=True,
        max_output_tokens=MAX_OUTPUT_TOKENS_OPUS,
        supports_extended_output_beta=True,
        context_window=1_000_000,
        supports_effort=True,
        supports_strict_tools=True,
        supports_xhigh_effort=True,
        supports_web_fetch=False,
        supports_thinking_display=True,
        supports_forced_tool_with_thinking=False,
        supports_json_output_format=True,
    ),
    MODEL_OPUS_5: ModelCapabilities(
        # Claude Opus 5 capability profile per Anthropic's models overview and
        # the Opus 4.8 → Opus 5 migration guide: 1M-token context window, 128k
        # max output, the ``output-300k-2026-03-24`` batch beta (the overview
        # names Opus 5 explicitly in the supported set), adaptive thinking, the
        # full effort ladder INCLUDING ``xhigh``, and structured outputs /
        # strict tool use. Every flag is confirmed against published docs — no
        # conservative placeholders.
        #
        # Two Opus 5 breaking changes do not affect this app: (1) an explicit
        # ``thinking={"type": "disabled"}`` 400s at effort ``xhigh``/``max``,
        # and this codebase never sends ``disabled`` — ``thinking_config_for``
        # omits the key instead; (2) omitting ``thinking`` now means adaptive
        # thinking is ON, but no Opus-routed phase opts out of thinking (only
        # Haiku triage and the Sonnet STRICT_STRUCTURED verification mode do).
        #
        # ``supports_web_fetch=False`` is the one place Opus 5 is genuinely
        # LESS capable than Opus 4.8 — see the flag's docstring. It is the
        # documented exception, not a conservative placeholder.
        supports_adaptive_thinking=True,
        max_output_tokens=MAX_OUTPUT_TOKENS_OPUS,
        supports_extended_output_beta=True,
        context_window=1_000_000,
        supports_effort=True,
        supports_strict_tools=True,
        supports_xhigh_effort=True,
        supports_web_fetch=False,
        supports_thinking_display=True,
        supports_forced_tool_with_thinking=True,
        supports_json_output_format=True,
    ),
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
        supports_web_fetch=True,
        supports_thinking_display=True,
        supports_forced_tool_with_thinking=True,
        supports_json_output_format=True,
    ),
    MODEL_SONNET_55: ModelCapabilities(
        # Claude Sonnet 5.5 per Anthropic's models overview and the Sonnet 5
        # → Sonnet 5.5 migration guide (checked 2026-09-29): same tokenizer,
        # 1M-token context window and 128k output as Sonnet 5, up to 300k on
        # the Message Batches API with the ``output-300k-2026-03-24`` beta,
        # adaptive thinking, all five effort levels (default ``high``, but
        # recalibrated — a level no longer means the same amount of thinking
        # as on Sonnet 5), structured outputs, strict tool use, and the same
        # server tools as Sonnet 5, web fetch included.
        #
        # Breaking changes that could reach this app: ``thinking: disabled``
        # 400s (never sent here) and forced ``tool_choice`` 400s (never sent
        # to a Sonnet phase; the EX-02 forced arm is gated off by
        # ``supports_forced_tool_with_thinking=False``).
        supports_adaptive_thinking=True,
        max_output_tokens=MAX_OUTPUT_TOKENS_SONNET_5,
        supports_extended_output_beta=True,
        context_window=1_000_000,
        supports_effort=True,
        supports_strict_tools=True,
        supports_xhigh_effort=True,
        supports_web_fetch=True,
        supports_thinking_display=True,
        supports_forced_tool_with_thinking=False,
        supports_json_output_format=True,
    ),
    MODEL_SONNET_5: ModelCapabilities(
        # Claude Sonnet 5 capability profile per Anthropic's models overview
        # and the Sonnet 5 migration guide: adaptive thinking (on by default
        # when the field is omitted — this app always sends it explicitly),
        # 1M-token context window, a 128k output ceiling (first Sonnet at
        # the Opus ceiling), the full effort range INCLUDING ``xhigh``
        # (first Sonnet-tier model with it), and structured outputs /
        # strict tool use. The 300k batch extended-output beta was previously
        # left off "pending confirmation against the beta's supported-model
        # list"; the models overview now confirms it explicitly — "on the
        # Message Batches API, Claude Opus 5, Opus 4.8, Opus 4.7, Opus 4.6,
        # Sonnet 5, and Sonnet 4.6 support up to 300k output tokens by using
        # the output-300k-2026-03-24 beta header" — so the placeholder is
        # resolved. Only reachable via SPEC_CRITIC_REVIEW_MODEL, since the
        # 300k beta rides the review batch alone.
        supports_adaptive_thinking=True,
        max_output_tokens=MAX_OUTPUT_TOKENS_SONNET_5,
        supports_extended_output_beta=True,
        context_window=1_000_000,
        supports_effort=True,
        supports_strict_tools=True,
        supports_xhigh_effort=True,
        supports_web_fetch=True,
        supports_thinking_display=True,
        supports_forced_tool_with_thinking=True,
        supports_json_output_format=True,
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
        supports_web_fetch=True,
        supports_forced_tool_with_thinking=True,
        supports_json_output_format=True,
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
        # Haiku 4.5 never receives ``thinking`` (it does not support adaptive
        # thinking and triage omits the key), so forcing the single triage
        # tool is a valid request shape here — see the flag's docstring.
        supports_forced_tool_choice=True,
        supports_json_output_format=True,
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


def model_supports_web_fetch(model: str) -> bool:
    """Whether ``model`` may be sent the ``web_fetch`` server tool.

    Not uniform across current models — Claude Opus 5 is the documented
    exception (see ``ModelCapabilities.supports_web_fetch``). The
    verification tool builder must consult this before attaching
    ``web_fetch_20260209``, or the escalation tier — which routes to Opus —
    sends a tool the model does not accept on the app's highest-stakes path.
    """
    return model_capabilities(model).supports_web_fetch


# Phase identifiers (declared above so the phase→budget registry can use
# them) gate per-phase request decisions. ``_PHASES_NO_THINKING`` is the
# extension point for phases that should never request thinking regardless
# of model capability — currently only the Haiku triage classifier, which
# is a shallow batch-classification pass.
_PHASES_NO_THINKING: frozenset[str] = frozenset({PHASE_TRIAGE})


# The documented ``thinking.display`` value that returns a readable summary of
# the model's reasoning (the other is "omitted", the default on current
# models). It changes what comes back, not what is billed.
THINKING_DISPLAY_SUMMARIZED = "summarized"


def deep_trace_recording() -> bool:
    """Whether a deep trace is recording right now (the global recorder is
    deep and its writer is running). Never raises; the tracing package is
    imported lazily, since it sits above ``core``."""
    try:
        from ..tracing.recorder import get_recorder

        recorder = get_recorder()
    except Exception:  # noqa: BLE001 — tracing never changes a request by failing
        return False
    if recorder is None or getattr(recorder, "is_deep", False) is not True:
        return False
    # A deep recorder whose writer has died records nothing, so it no longer
    # changes a request (objects without the attribute are taken as alive).
    return getattr(recorder, "writer_alive", True) is not False


def thinking_config_for(*, model: str, phase: str) -> dict | None:
    """Return the ``thinking`` request parameter for ``(model, phase)``.

    Returns ``None`` when the parameter should be omitted entirely —
    either the phase opts out, or the model does not support adaptive
    thinking. Callers should branch on ``is None``; the Anthropic API
    rejects ``thinking=null``.

    While a deep trace is recording, a model that accepts
    ``thinking.display`` is asked for ``"summarized"`` thinking, so the
    trace's thinking events hold text instead of the empty strings the
    default ("omitted") returns (plan WP-13). That is the only change: an
    ordinary run's request is byte-identical, a request that omits
    ``thinking`` still omits it, and the setting changes what is visible,
    not what is billed.
    """
    if phase in _PHASES_NO_THINKING:
        return None
    if not model_supports_adaptive_thinking(model):
        return None
    config = {"type": "adaptive"}
    if model_capabilities(model).supports_thinking_display and deep_trace_recording():
        config["display"] = THINKING_DISPLAY_SUMMARIZED
    return config


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
# ``low`` / ``medium`` / ``high`` / ``xhigh`` (plus ``max``).
#
# ``high`` is the ceiling this app uses. The deep-reasoning phases (review,
# cross-check, compliance) previously declared ``xhigh``; they were lowered
# to ``high`` as a token-spend measure, ``high`` being the level Anthropic
# describes as the balance point between quality and token efficiency.
# ``max`` was never used (it overshoots without a measured benefit for this
# workload) and verification stays at medium so the verdict envelope doesn't
# balloon. Nothing above ``high`` is declared by any phase today.
#
# **Opus runs at ``medium``.** Every request to a model in ``OPUS_MODELS``
# resolves to ``OPUS_EFFORT_CEILING`` (``medium``) when its phase default is
# higher — today the per-spec review and the verification escalation tier,
# both ``high`` before. This is an operator decision, made with the move to
# Opus 5.5: Anthropic's Opus 5.5 migration guide reports that, in its
# testing, Opus 5.5 at ``medium`` exceeds Opus 5 at ``high`` on coding and
# knowledge-work evaluations, and ``medium`` is Opus 5.5's own API default.
# It has not been measured on this app's workload (plan EX-03 is where such a
# comparison lives). The ceiling is by model, not by phase, so a Sonnet phase
# keeps its level and a ``SPEC_CRITIC_*_MODEL`` override that routes a phase
# to Opus runs it at ``medium`` too. An explicit ``effort_override`` (the
# verification mode, the EX-03 review switch) is not capped: it is a
# deliberate request for a level.
#
# The ``xhigh`` gate below is retained deliberately, because the ceiling is a
# tuning decision that may be revisited. ``xhigh`` is not universal: Opus
# 5.5, Opus 5, Opus 4.8, Sonnet 5.5 and Sonnet 5 accept it; Sonnet 4.6's
# supported set is ``{low, medium, high, max}`` and it rejects ``xhigh`` at
# submit with a 400 ("This model does not support effort level 'xhigh'"). So ``supports_effort`` being
# a coarse boolean is not enough: any phase that declares ``xhigh`` while
# running on a model without ``supports_xhigh_effort`` (e.g. cross-check
# under a pinned Sonnet 4.6) must clamp down to ``high`` or the request
# fails. :func:`effort_config_for` does that clamp via
# :func:`_clamp_effort_for_model`, so restoring ``xhigh`` on a phase is a
# one-line change that stays safe on every registered model.
#
# Effort is a request-policy decision, not a prompt one. Centralizing it
# here keeps every request site (review / batch review / cross-check /
# verification / retry / continuation) reaching for the same lever via
# :func:`apply_effort_config`. Unsupported models silently omit the
# parameter via :func:`model_supports_effort`.
#
# Default policy:
#
# - Verification (PHASE_VERIFICATION{,_RETRY,_CONTINUATION}): medium, on
#   the Sonnet initial pass and the Opus escalation tier alike. (Sonnet 5.5's
#   effort levels are recalibrated from Sonnet 5's; Anthropic's guidance
#   starts multistep tool use at ``medium``.) The STRICT_STRUCTURED
#   verification mode overrides this to ``low`` — its cost lever is effort,
#   not thinking (see :mod:`src.verification.verification_modes`); the mode
#   passes the level through ``effort_override`` so the model clamp still
#   applies.
# - Per-spec review (PHASE_REVIEW): high by phase, which the Opus ceiling
#   makes medium on the default Opus review model.
# - Cross-check, compliance (Sonnet): high.
# - Research / drawing impact (Sonnet): high. Drawing digest: medium.
# - Any Opus request: at most medium (``OPUS_EFFORT_CEILING``).
# - Triage (Haiku): omit (Haiku does not support effort).
# - Unknown model: omit.

EFFORT_LOW = "low"
EFFORT_MEDIUM = "medium"
EFFORT_HIGH = "high"
EFFORT_XHIGH = "xhigh"

# Phases whose request paths route through ``output_config.effort``. Triage
# is intentionally omitted — it defaults to Haiku which does not support
# effort, and the workload is a small classification pass that does not
# benefit from elevated effort.
_PHASE_DEFAULT_EFFORT: dict[str, str] = {
    PHASE_REVIEW: EFFORT_HIGH,
    PHASE_CROSS_CHECK: EFFORT_HIGH,
    PHASE_VERIFICATION: EFFORT_MEDIUM,
    PHASE_VERIFICATION_RETRY: EFFORT_MEDIUM,
    PHASE_VERIFICATION_CONTINUATION: EFFORT_MEDIUM,
    # Research is retrieval-heavy: ``high`` keeps the model persistent
    # about chasing primary sources without the token eagerness of the
    # levels above it.
    PHASE_RESEARCH: EFFORT_HIGH,
    # Compliance is a deep-evaluation pass like cross-check, and tracks
    # it at ``high``.
    PHASE_COMPLIANCE: EFFORT_HIGH,
    # The drawing digest reads and transcribes documents it was handed —
    # no tools to chase, no deep reasoning; ``medium`` keeps the output
    # disciplined against the per-chunk length contract.
    PHASE_DRAWING_DIGEST: EFFORT_MEDIUM,
    # Drawing-impact synthesis reasons about how the digest relates to the
    # findings — a genuine (if bounded) reasoning task; ``high`` keeps it
    # grounded.
    PHASE_DRAWING_IMPACT: EFFORT_HIGH,
}

# Effort levels this app uses, least to most. Used to apply a ceiling; a
# level missing from it (``max``, never declared) is left to the model clamp.
_EFFORT_ORDER: tuple[str, ...] = (EFFORT_LOW, EFFORT_MEDIUM, EFFORT_HIGH, EFFORT_XHIGH)

# The most effort an Opus request resolves to from a phase default (see the
# section comment above). Opus 5.5's own API default.
OPUS_EFFORT_CEILING = EFFORT_MEDIUM


def _apply_opus_effort_ceiling(level: str, model: str) -> str:
    """Lower ``level`` to :data:`OPUS_EFFORT_CEILING` on an Opus model.

    Levels at or below the ceiling, and every non-Opus model, pass through.
    """
    if model not in OPUS_MODELS or level not in _EFFORT_ORDER:
        return level
    if _EFFORT_ORDER.index(level) > _EFFORT_ORDER.index(OPUS_EFFORT_CEILING):
        return OPUS_EFFORT_CEILING
    return level

# Effort levels only ``supports_xhigh_effort`` models accept (Opus 5.5, Opus 5,
# Opus 4.8, Sonnet 5.5, Sonnet 5). Sonnet 4.6's supported set is ``{low, medium, high, max}``; it
# rejects ``xhigh`` at submit with a 400 ("This model does not support effort
# level 'xhigh'"). Membership in this set is the trigger for
# :func:`_clamp_effort_for_model` to downgrade to ``high`` on a model whose
# capability entry lacks the flag. Adding a future gated level here makes
# every phase clamp it automatically on non-supporting models.
_XHIGH_GATED_EFFORT_LEVELS: frozenset[str] = frozenset({EFFORT_XHIGH})


def _clamp_effort_for_model(level: str, model: str) -> str:
    """Clamp an effort ``level`` down to what ``model`` accepts.

    ``xhigh`` requires the capability whitelist's ``supports_xhigh_effort``
    flag (Opus 5.5, Opus 5, Opus 4.8, Sonnet 5.5, Sonnet 5); on any other model it falls back to
    ``high`` — the deepest level Sonnet 4.6 accepts (we don't use ``max``). Every
    other level passes through unchanged, so with every phase at ``high`` or
    below this is a pass-through. It stays wired as the guard for any future
    phase that declares ``xhigh`` again: that phase cannot 400 at submit when
    an env override pins a model without the flag, and unknown ids clamp too
    since the conservative default capabilities leave it off.
    """
    if (
        level in _XHIGH_GATED_EFFORT_LEVELS
        and not model_capabilities(model).supports_xhigh_effort
    ):
        return EFFORT_HIGH
    return level


def effort_config_for(
    *, model: str, phase: str, effort_override: str | None = None
) -> dict | None:
    """Return the ``output_config`` dict for ``(model, phase)``, or ``None``.

    Returns ``None`` (i.e. "omit the field") when:

    - the model does not support effort (Haiku, unknown / future models),
    - the phase has no registered default (triage — defaults to Haiku,
      which already short-circuits above).

    Otherwise returns ``{"effort": <level>}`` where the level is the phase
    default from :data:`_PHASE_DEFAULT_EFFORT`, lowered to
    :data:`OPUS_EFFORT_CEILING` (``medium``) on an Opus model, then clamped
    to what ``model`` supports (see :func:`_clamp_effort_for_model`). No
    phase declares a level above ``high`` today, so the clamp is currently
    inert — it stays wired so raising a phase's ceiling again cannot 400 at
    submit.

    ``effort_override`` lets a caller whose policy is finer-grained than
    the phase — the verification *mode* (``ModePolicy.effort``) and the
    EX-03 review switch — pin a level explicitly. It wins over both the
    phase default and the Opus ceiling, but never over the capability
    gates: a model that doesn't support effort still omits the field, and
    the level still passes through :func:`_clamp_effort_for_model` so a
    gated level cannot 400 under a pinned-model override. ``None`` (the
    default) keeps the phase-only resolution.
    """
    if not model_supports_effort(model):
        return None

    if effort_override:
        return {"effort": _clamp_effort_for_model(effort_override, model)}

    level = _PHASE_DEFAULT_EFFORT.get(phase)
    if level is None:
        return None
    level = _apply_opus_effort_ceiling(level, model)
    return {"effort": _clamp_effort_for_model(level, model)}


def apply_effort_config(
    kwargs: dict, *, model: str, phase: str, effort_override: str | None = None
) -> dict:
    """Insert ``output_config`` into ``kwargs`` only when applicable.

    Mutates and returns ``kwargs`` for fluent use. The key is omitted
    entirely (not set to ``None``) when effort is not applicable, because
    the Anthropic API rejects ``output_config=null``.

    Mirrors :func:`apply_thinking_config` so request builders pair the
    two helpers the same way per directive 4 ("Pair effort decisions
    with thinking decisions where appropriate"). ``effort_override`` is
    forwarded verbatim to :func:`effort_config_for`.
    """
    config = effort_config_for(model=model, phase=phase, effort_override=effort_override)
    if config is not None:
        kwargs["output_config"] = config
    return kwargs


# ---------------------------------------------------------------------------
# Experiment EX-03: the per-spec review's effort (default off)
# ---------------------------------------------------------------------------
#
# ``SPEC_CRITIC_REVIEW_EFFORT`` sets the effort of the per-spec review
# (``PHASE_REVIEW``) and of nothing else: not cross-check, compliance, or any
# verification phase. It exists so plan EX-03 can compare the review's
# default with one other level by changing the environment alone. (That
# default is ``medium`` on the Opus review model since the Opus effort
# ceiling; it was ``high`` when EX-03 was written.) The decision record is
# ``plans/experiments/EX-03-model-effort-confidence.md``. The override is not
# subject to the Opus ceiling: asking for ``high`` or ``xhigh`` gets it.
#
# Values: ``low`` / ``medium`` / ``high`` / ``xhigh``, the levels this module
# defines. Unset, empty, or ``0`` / ``false`` / ``no`` / ``off`` leaves the
# phase default in place and every request byte-identical. Any other value is
# the default too, with one warning: an experiment switch fails closed. The
# level still passes through the capability gate and the per-model clamp
# (``effort_config_for``), so ``xhigh`` under a pinned Sonnet 4.6 review model
# is sent as ``high``, and a model without effort support sends none.

ENV_REVIEW_EFFORT = "SPEC_CRITIC_REVIEW_EFFORT"

_REVIEW_EFFORT_VALUES = frozenset({EFFORT_LOW, EFFORT_MEDIUM, EFFORT_HIGH, EFFORT_XHIGH})
_WARNED_REVIEW_EFFORT_VALUES: set[str] = set()


def review_effort_override() -> str | None:
    """The review effort the environment asks for, or ``None`` for the default.

    Read at call time, so an evaluation arm switches with the environment
    alone. See the section comment above for the values.
    """
    raw = os.environ.get(ENV_REVIEW_EFFORT)
    if raw is None:
        return None
    val = raw.strip().lower()
    if val == "" or val in _DISABLE_TOKENS:
        return None
    if val in _REVIEW_EFFORT_VALUES:
        return val
    if val not in _WARNED_REVIEW_EFFORT_VALUES:
        _WARNED_REVIEW_EFFORT_VALUES.add(val)
        _log.warning(
            "%s=%r is not a recognized value (use low, medium, high, or xhigh); "
            "the review keeps its default effort.",
            ENV_REVIEW_EFFORT,
            raw,
        )
    return None


# ---------------------------------------------------------------------------
# Prompt caching (centralized phase-aware policy)
# ---------------------------------------------------------------------------
#
# Each phase declares whether its system prompt and tool list are stable /
# large / repeated enough to benefit from caching. Caching is enabled for
# high-value phases (review, batch review, cross-check, verification +
# retry/continuation) and disabled for triage, whose stable prefix is far
# below the 4,096-token Haiku 4.5 cache minimum: the API would silently
# ignore the breakpoint (no cache entry, no error), so it would only add
# noise to the request.


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
    # Triage: a ~375-token system prompt plus one small tool definition,
    # called in batches of up to 20 findings — a prefix far below the
    # 4,096-token Haiku 4.5 cache minimum, so a breakpoint would be ignored
    # and repeated calls could never hit. No breakpoints at all.
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

    Always True. The GUI also asks the count API for the largest spec's
    estimate when the file list changes; the pipeline call here is the
    moment-of-truth guard before a real submission. The endpoint returns the
    provider's estimate, not an exact count (plan WP-08).
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
# Every entry carries a category so the evidence panel can explain *why* a
# model-cited URL was rejected ("blocked domain: social media") instead of the
# bare "ungrounded" — see ``blocked_domain_category`` and
# ``source_grounding.describe_rejection``. The labels are rendered verbatim in
# the DOCX / HTML reports and persist inside cached verdicts' rejection
# explanations, so treat them as stable strings.
#
# The entries live in ONE ordered tuple (annotated, not regrouped) so the flat
# ``blocked_domains`` list the two server-tool builders emit stays
# byte-identical in content and order — it sits inside the prompt-cache
# prefix. Any change to this list should be exercised against the verifier's
# grounding tests in ``tests/test_source_grounding_invariant.py``.
BLOCKED_DOMAIN_CATEGORY_AGGREGATOR = "user-generated Q&A / aggregator"
BLOCKED_DOMAIN_CATEGORY_LLM_OUTPUT = "LLM-assistant output"
BLOCKED_DOMAIN_CATEGORY_TRADE_FORUM = "trade forum"
BLOCKED_DOMAIN_CATEGORY_MARKETPLACE = "lead-generation marketplace"
BLOCKED_DOMAIN_CATEGORY_CONTENT_FARM = "DIY / content farm"
BLOCKED_DOMAIN_CATEGORY_SOCIAL = "social media"
BLOCKED_DOMAIN_CATEGORY_ENCYCLOPEDIA = "general encyclopedia"

_WEB_SEARCH_BLOCKED_DOMAIN_ENTRIES: tuple[tuple[str, str], ...] = (
    # User-generated Q&A / aggregators: contractor-grade evidence is rare.
    ("reddit.com", BLOCKED_DOMAIN_CATEGORY_AGGREGATOR),
    ("quora.com", BLOCKED_DOMAIN_CATEGORY_AGGREGATOR),
    ("medium.com", BLOCKED_DOMAIN_CATEGORY_AGGREGATOR),
    ("stackexchange.com", BLOCKED_DOMAIN_CATEGORY_AGGREGATOR),
    ("stackoverflow.com", BLOCKED_DOMAIN_CATEGORY_AGGREGATOR),
    ("answers.yahoo.com", BLOCKED_DOMAIN_CATEGORY_AGGREGATOR),
    ("fixya.com", BLOCKED_DOMAIN_CATEGORY_AGGREGATOR),
    # LLM-assistant outputs: another model's answer is not a citable source.
    ("chatgpt.com", BLOCKED_DOMAIN_CATEGORY_LLM_OUTPUT),
    ("perplexity.ai", BLOCKED_DOMAIN_CATEGORY_LLM_OUTPUT),
    ("openai.com", BLOCKED_DOMAIN_CATEGORY_LLM_OUTPUT),
    ("gemini.google.com", BLOCKED_DOMAIN_CATEGORY_LLM_OUTPUT),
    ("claude.ai", BLOCKED_DOMAIN_CATEGORY_LLM_OUTPUT),
    ("you.com", BLOCKED_DOMAIN_CATEGORY_LLM_OUTPUT),
    ("phind.com", BLOCKED_DOMAIN_CATEGORY_LLM_OUTPUT),
    ("copilot.microsoft.com", BLOCKED_DOMAIN_CATEGORY_LLM_OUTPUT),
    ("poe.com", BLOCKED_DOMAIN_CATEGORY_LLM_OUTPUT),
    ("character.ai", BLOCKED_DOMAIN_CATEGORY_LLM_OUTPUT),
    ("jasper.ai", BLOCKED_DOMAIN_CATEGORY_LLM_OUTPUT),
    ("writesonic.com", BLOCKED_DOMAIN_CATEGORY_LLM_OUTPUT),
    # Trade forums: useful peer chatter, not authoritative for code compliance.
    ("diychatroom.com", BLOCKED_DOMAIN_CATEGORY_TRADE_FORUM),
    ("forums.jlconline.com", BLOCKED_DOMAIN_CATEGORY_TRADE_FORUM),
    ("hvac-talk.com", BLOCKED_DOMAIN_CATEGORY_TRADE_FORUM),
    ("inspectionnews.net", BLOCKED_DOMAIN_CATEGORY_TRADE_FORUM),
    ("inspectorsforum.com", BLOCKED_DOMAIN_CATEGORY_TRADE_FORUM),
    ("contractortalk.com", BLOCKED_DOMAIN_CATEGORY_TRADE_FORUM),
    # DIY / home-improvement content farms, with the lead-generation
    # marketplaces that sit among them (the flat order is preserved).
    ("doityourself.com", BLOCKED_DOMAIN_CATEGORY_CONTENT_FARM),
    ("homeadvisor.com", BLOCKED_DOMAIN_CATEGORY_MARKETPLACE),
    ("thumbtack.com", BLOCKED_DOMAIN_CATEGORY_MARKETPLACE),
    ("angi.com", BLOCKED_DOMAIN_CATEGORY_MARKETPLACE),
    ("ehow.com", BLOCKED_DOMAIN_CATEGORY_CONTENT_FARM),
    ("wikihow.com", BLOCKED_DOMAIN_CATEGORY_CONTENT_FARM),
    ("about.com", BLOCKED_DOMAIN_CATEGORY_CONTENT_FARM),
    ("thespruce.com", BLOCKED_DOMAIN_CATEGORY_CONTENT_FARM),
    ("bobvila.com", BLOCKED_DOMAIN_CATEGORY_CONTENT_FARM),
    ("familyhandyman.com", BLOCKED_DOMAIN_CATEGORY_CONTENT_FARM),
    ("hunker.com", BLOCKED_DOMAIN_CATEGORY_CONTENT_FARM),
    ("sapling.com", BLOCKED_DOMAIN_CATEGORY_CONTENT_FARM),
    ("reference.com", BLOCKED_DOMAIN_CATEGORY_CONTENT_FARM),
    ("leaf.tv", BLOCKED_DOMAIN_CATEGORY_CONTENT_FARM),
    ("sciencing.com", BLOCKED_DOMAIN_CATEGORY_CONTENT_FARM),
    ("bizfluent.com", BLOCKED_DOMAIN_CATEGORY_CONTENT_FARM),
    ("pocketsense.com", BLOCKED_DOMAIN_CATEGORY_CONTENT_FARM),
    # Social: unsuitable for a defensible engineering review.
    ("facebook.com", BLOCKED_DOMAIN_CATEGORY_SOCIAL),
    ("twitter.com", BLOCKED_DOMAIN_CATEGORY_SOCIAL),
    ("x.com", BLOCKED_DOMAIN_CATEGORY_SOCIAL),
    ("instagram.com", BLOCKED_DOMAIN_CATEGORY_SOCIAL),
    ("tiktok.com", BLOCKED_DOMAIN_CATEGORY_SOCIAL),
    ("linkedin.com", BLOCKED_DOMAIN_CATEGORY_SOCIAL),
    ("pinterest.com", BLOCKED_DOMAIN_CATEGORY_SOCIAL),
    ("youtube.com", BLOCKED_DOMAIN_CATEGORY_SOCIAL),
    ("threads.net", BLOCKED_DOMAIN_CATEGORY_SOCIAL),
    # General encyclopedias (tertiary). ``wikipedia.org`` already covers
    # every subdomain (``simple.wikipedia.org``, ``en.wikipedia.org``, ...).
    ("wikipedia.org", BLOCKED_DOMAIN_CATEGORY_ENCYCLOPEDIA),
    ("britannica.com", BLOCKED_DOMAIN_CATEGORY_ENCYCLOPEDIA),
)

# The flat list the ``web_search`` / ``web_fetch`` tool dicts emit — derived
# from the annotated entries so the two can never drift.
_WEB_SEARCH_BLOCKED_DOMAINS = [
    domain for domain, _category in _WEB_SEARCH_BLOCKED_DOMAIN_ENTRIES
]


def _blocked_domain_host(url: str | None) -> str:
    """Lowercased hostname of ``url`` (bare ``host/path`` accepted), or ``""``."""
    if not url or not isinstance(url, str):
        return ""
    cleaned = url.strip()
    if not cleaned:
        return ""
    try:
        parts = urlsplit(cleaned)
        if not parts.scheme and not parts.netloc and parts.path:
            # ``reddit.com/r/hvac`` parses as a bare path; assume https.
            parts = urlsplit("https://" + cleaned)
        host = parts.hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".")


def blocked_domain_category(url: str | None) -> str | None:
    """The blocklist category of ``url``'s host, or ``None`` when not blocked.

    Mirrors the server tools' own matching rule — an entry covers its apex
    and every subdomain — so a citation the tools would never have returned
    can be explained as "blocked domain: <category>" rather than merely
    "ungrounded". A URL whose host cannot be parsed is not blocked.
    """
    host = _blocked_domain_host(url)
    if not host:
        return None
    for domain, category in _WEB_SEARCH_BLOCKED_DOMAIN_ENTRIES:
        if host == domain or host.endswith("." + domain):
            return category
    return None

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

    ``user_location`` steers search localization. The engine has **no**
    location opinion of its own: when nothing is supplied the key is
    omitted entirely, so the API searches un-localized. The two callers
    that do have an opinion supply it explicitly — a run with a
    :class:`~src.core.project_profile.ProjectProfile` passes
    ``profile.web_search_user_location()`` (research WS-3, verification
    WS-4), and a profile-less verification run gets the owning module's
    ``ReviewModule.default_web_search_user_location`` from the routing
    layer (:func:`src.verification.verification_routing.build_verification_tools_from_decision`),
    which is how the California module keeps its long-standing
    California-localized tool dict byte-identical while a data-center
    module without a profile searches nowhere in particular instead of
    being silently steered to California.
    """
    tool = {
        "type": "web_search_20260209",
        "name": "web_search",
        "blocked_domains": list(_WEB_SEARCH_BLOCKED_DOMAINS),
        "max_uses": max_uses,
    }
    if user_location:
        tool["user_location"] = dict(user_location)
    return tool


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
# Code-execution container continuity (``pause_turn`` resumes)
# ---------------------------------------------------------------------------
#
# ``web_search_20260209`` / ``web_fetch_20260209`` default their
# ``allowed_callers`` to ``["code_execution_20260120"]``: dynamic filtering
# runs the search *from inside a code-execution container* that the API
# provisions for the request automatically (both web tools share one
# container). That container is invisible in the happy path — the tool result
# blocks arrive inline and the turn ends.
#
# It stops being invisible the moment a turn PAUSES mid-filter. The paused
# assistant content then carries tool uses generated by code execution that
# have not completed, and the continuation request must name the container
# they belong to. Resending only ``messages`` — the documented shape for a
# plain ``pause_turn`` resume — is rejected with HTTP 400:
#
#     container_id is required when there are pending tool uses generated by
#     code execution with tools.
#
# This killed whole research dimensions in production: a dimension that
# searched hard enough to pause lost every token it had already spent,
# because the resume that would have banked the work could not be issued.
#
# The contract is a top-level ``container`` request parameter carrying the
# **bare string id** from the prior response's ``container`` object (NOT a
# ``{"id": ...}`` wrapper — that is the response shape, not the request
# shape). Per Anthropic's code-execution tool docs a container is
# checkpointed after ~5 minutes of inactivity and *restored* by any request
# naming it within the 30-day expiry window, so this is safe across the long
# gaps between batch verification waves, not just tight real-time loops.
#
# Every ``pause_turn`` resume site threads these two helpers: the research
# fan-out loop, the real-time verifier loop, and the batch continuation
# builder. A site that forgets them does not degrade — it 400s.


def container_id_from_response(response) -> str | None:
    """Return the code-execution container id carried by ``response``.

    ``None`` when the response has no container (no dynamic filtering ran, a
    mocked/legacy object, or any access error). Defensive by construction for
    the same reason :func:`extract_cache_diagnostics` is: reading transport
    metadata must never sink the call whose continuation it exists to enable.

    The id is read from the response's top-level ``container.id``. Callers
    should keep the last non-``None`` value across a multi-turn conversation
    rather than overwriting with each response — a turn that ran no code
    execution reports no container, and the conversation still belongs to the
    container an earlier turn created.
    """
    try:
        container = getattr(response, "container", None)
        if container is None and isinstance(response, dict):
            container = response.get("container")
        if container is None:
            return None
        if isinstance(container, str):
            # Already an id (an echoed request param rather than a response
            # object). Accept it so a caller can round-trip its own value.
            return container or None
        if isinstance(container, dict):
            container_id = container.get("id")
        else:
            container_id = getattr(container, "id", None)
        if not container_id:
            return None
        return str(container_id)
    except Exception:
        return None


def apply_container_config(params: dict, container_id: str | None) -> None:
    """Attach ``container`` to a continuation request body, when one is known.

    Mutates ``params`` in place (the ``apply_*_config`` convention used by
    :func:`apply_thinking_config` / :func:`apply_effort_config`). A falsy
    ``container_id`` writes nothing, so an initial request — and every request
    in a conversation that never provisioned a container — keeps a
    byte-identical body.

    The value is the bare id string, which is what the Messages API expects in
    the top-level ``container`` parameter.
    """
    if container_id:
        params["container"] = container_id


# Request-level automatic caching for a ``pause_turn`` resume. A resume
# re-sends the whole accumulated assistant turn (thinking, server-tool uses
# and their results), and the next resume re-sends it again unchanged; with
# breakpoints only on ``system`` and ``tools`` the conversation tail has no
# read point, so every resume re-prices it at full input rate. The top-level
# field is used instead of an explicit marker on the last block because that
# block is usually a server-tool result or a thinking block, neither of which
# accepts a marker — the API walks back to the nearest eligible block itself.
# Default five-minute TTL on purpose: a resume follows its pause within
# seconds, and under five minutes the one-hour TTL buys nothing but the
# doubled write price (diagnostics already price five-minute writes at 1.25×).
# Scoped to the two real-time resume loops; the batch wave path is not
# touched (waves can be hours apart, past any TTL).
RESUME_TAIL_CACHE_CONTROL: dict = {"type": "ephemeral"}


def _message_role(message) -> str | None:
    if isinstance(message, dict):
        return message.get("role")
    return getattr(message, "role", None)


def apply_resume_cache_config(params: dict, messages) -> None:
    """Attach request-level ``cache_control`` to a ``pause_turn`` resume.

    Mutates ``params`` in place (the ``apply_*_config`` convention). Writes
    nothing unless ``messages`` already carries an assistant turn — i.e. the
    request IS a resume — so the first call of every conversation (the
    common, single-shot path) keeps a byte-identical body and never pays a
    cache write on a user message nothing will re-read.
    """
    if any(_message_role(m) == "assistant" for m in (messages or ())):
        params["cache_control"] = dict(RESUME_TAIL_CACHE_CONTROL)


# ---------------------------------------------------------------------------
# Cache-token usage extraction (for diagnostics)
# ---------------------------------------------------------------------------

# ``cache_creation_breakdown_status`` values. The status is carried alongside
# the counters so a reader can tell *why* tokens are unknown rather than
# inferring it from a zero, which is the distinction the whole breakdown
# exists to preserve.
CACHE_BREAKDOWN_NONE = "none"            # no cache writes on this call
CACHE_BREAKDOWN_COMPLETE = "complete"    # detail present and sums to the aggregate
CACHE_BREAKDOWN_ABSENT = "absent"        # no per-TTL detail offered at all
CACHE_BREAKDOWN_PARTIAL = "partial"      # detail present but under-counts the aggregate
CACHE_BREAKDOWN_INCONSISTENT = "inconsistent"  # detail contradicts the aggregate


def _usage_field(source, name):
    """Read ``name`` off an SDK object or a plain dict, or ``None``."""
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _nonneg_int(value) -> int | None:
    """Coerce to a non-negative int, or ``None`` when untrustworthy.

    A negative or unparseable count is not zero — it is a signal that the
    detail cannot be relied on, and saying so is the difference between an
    accounting warning and a silently wrong bill.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def extract_cache_usage(usage) -> dict:
    """Pull cache-related fields off an Anthropic usage object or dict.

    Returns the two aggregate counters (``cache_creation_input_tokens`` /
    ``cache_read_input_tokens``) plus a per-TTL breakdown of the writes.

    **Why the breakdown matters.** A 5-minute cache write bills at 1.25× the
    base input rate and a 1-hour write at 2×. This app declares a 1-hour TTL on
    every breakpoint it sets, so pricing every write at 2× looks safe — but
    server tools insert their *own* 5-minute breakpoint after tool results when
    the request already uses caching. Those writes bill at 1.25× and land
    disproportionately in verification, the phase that runs the most web
    searches. Pricing them at 2× overstates the bill, which is the safe
    direction to be wrong in but is still wrong.

    **The invariant this maintains is
    ``known_5m + known_1h + unknown == aggregate``.** Missing detail becomes
    *unknown*, never zero, and the aggregate is never added to its own
    components as separate spend. Untrustworthy detail (negative, malformed,
    or summing past the aggregate) is discarded in favour of the aggregate it
    contradicts — the paid total is the number we actually know, so
    normalization degrades the breakdown rather than the spend.
    """
    aggregate = _nonneg_int(_usage_field(usage, "cache_creation_input_tokens")) or 0
    read = _nonneg_int(_usage_field(usage, "cache_read_input_tokens")) or 0

    detail = _usage_field(usage, "cache_creation")
    raw_5m = _nonneg_int(_usage_field(detail, "ephemeral_5m_input_tokens"))
    raw_1h = _nonneg_int(_usage_field(detail, "ephemeral_1h_input_tokens"))

    if aggregate == 0:
        known_5m = known_1h = unknown = 0
        status = CACHE_BREAKDOWN_NONE
    elif raw_5m is None and raw_1h is None:
        # No detail offered (older SDK, batch dict shape, or a provider that
        # does not report it). Every token is honestly unknown.
        known_5m = known_1h = 0
        unknown = aggregate
        status = CACHE_BREAKDOWN_ABSENT
    else:
        known_5m = raw_5m or 0
        known_1h = raw_1h or 0
        total_known = known_5m + known_1h
        if total_known > aggregate:
            # The detail claims more than was billed. Trust the aggregate and
            # discard the breakdown rather than invent spend that never
            # happened.
            known_5m = known_1h = 0
            unknown = aggregate
            status = CACHE_BREAKDOWN_INCONSISTENT
        elif total_known == aggregate:
            unknown = 0
            status = CACHE_BREAKDOWN_COMPLETE
        else:
            unknown = aggregate - total_known
            status = CACHE_BREAKDOWN_PARTIAL

    return {
        "cache_creation_input_tokens": aggregate,
        "cache_read_input_tokens": read,
        "cache_creation_5m_input_tokens": known_5m,
        "cache_creation_1h_input_tokens": known_1h,
        "cache_creation_unknown_input_tokens": unknown,
        "cache_creation_breakdown_status": status,
    }


# The token counters every cache-usage carrier holds. Kept as one tuple so a
# carrier, a merge, and a pricing call cannot drift apart on which keys exist.
CACHE_USAGE_TOKEN_KEYS = (
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens",
    "cache_creation_unknown_input_tokens",
)
CACHE_BREAKDOWN_STATUS_KEY = "cache_creation_breakdown_status"
# The subset :func:`src.core.pricing.estimate_cost_breakdown` accepts.
CACHE_PRICING_KEYS = CACHE_USAGE_TOKEN_KEYS


def derive_cache_breakdown_status(
    *, aggregate: int, known_5m: int, known_1h: int, unknown: int
) -> str:
    """Classify a set of cache-write counters.

    Derived from the counters rather than carried alongside them, so a status
    can never contradict the numbers it describes — which is the failure mode
    that matters when counters are summed across calls.
    """
    if aggregate <= 0:
        return CACHE_BREAKDOWN_NONE
    if unknown <= 0:
        return CACHE_BREAKDOWN_COMPLETE
    if (known_5m + known_1h) <= 0:
        return CACHE_BREAKDOWN_ABSENT
    return CACHE_BREAKDOWN_PARTIAL


def empty_cache_usage() -> dict:
    """A zeroed cache-usage dict — the shape every carrier defaults to."""
    out: dict = {key: 0 for key in CACHE_USAGE_TOKEN_KEYS}
    out[CACHE_BREAKDOWN_STATUS_KEY] = CACHE_BREAKDOWN_NONE
    return out


def cache_usage_from(source) -> dict:
    """Read a cache-usage dict off any carrier — dataclass, dict, or ``None``.

    Every carrier in the app (``ReviewResult``, ``VerificationResult``,
    ``_DimensionOutcome``, a ``call_usage`` entry, a diagnostics event dict)
    exposes the same field names, so one reader serves all of them and a new
    carrier needs no new accessor.

    A source carrying **none** of the split fields is not a carrier at all —
    it is a raw provider ``usage`` block, whose split lives under
    ``cache_creation.ephemeral_*``. Delegating to :func:`extract_cache_usage`
    there is what stops a raw block from reading as "aggregate N, split
    complete at zero", which would both break the accounting invariant and
    price every one of those writes at the wrong rate.
    """
    if source is None:
        return empty_cache_usage()
    aggregate = _nonneg_int(_usage_field(source, "cache_creation_input_tokens")) or 0
    read = _nonneg_int(_usage_field(source, "cache_read_input_tokens")) or 0
    raw_5m = _nonneg_int(_usage_field(source, "cache_creation_5m_input_tokens"))
    raw_1h = _nonneg_int(_usage_field(source, "cache_creation_1h_input_tokens"))
    raw_unknown = _nonneg_int(
        _usage_field(source, "cache_creation_unknown_input_tokens")
    )
    if raw_5m is None and raw_1h is None and raw_unknown is None:
        return extract_cache_usage(source)

    known_5m = raw_5m or 0
    known_1h = raw_1h or 0
    unknown = raw_unknown or 0
    carried_status = _usage_field(source, CACHE_BREAKDOWN_STATUS_KEY)
    if known_5m + known_1h + unknown != aggregate:
        # The split does not reconcile with the carrier's own aggregate.
        # Either way keep the aggregate — that is the number actually billed —
        # and classify all of it unknown, so the estimate stays conservative
        # rather than inventing or losing spend. What the two cases differ on
        # is the *label*, and that distinction is load-bearing: a carrier
        # still holding the default ``none`` status was populated by a caller
        # that set only the aggregate (a legacy path, or a hand-built result),
        # which is ``absent`` detail, not a contradiction. Calling that
        # ``inconsistent`` would fire the accounting warning on the ordinary
        # case and leave it meaning nothing when a real contradiction arrives.
        known_5m = known_1h = 0
        unknown = aggregate
        if not aggregate:
            status = CACHE_BREAKDOWN_NONE
        elif carried_status in (None, "", CACHE_BREAKDOWN_NONE):
            status = CACHE_BREAKDOWN_ABSENT
        else:
            status = CACHE_BREAKDOWN_INCONSISTENT
    else:
        status = derive_cache_breakdown_status(
            aggregate=aggregate,
            known_5m=known_5m,
            known_1h=known_1h,
            unknown=unknown,
        )
        # Restore the one status a derivation cannot see: ``inconsistent``
        # describes detail the extractor already discarded, so the counters
        # no longer carry the evidence for it.
        if carried_status == CACHE_BREAKDOWN_INCONSISTENT and aggregate:
            status = CACHE_BREAKDOWN_INCONSISTENT
    return {
        "cache_creation_input_tokens": aggregate,
        "cache_read_input_tokens": read,
        "cache_creation_5m_input_tokens": known_5m,
        "cache_creation_1h_input_tokens": known_1h,
        "cache_creation_unknown_input_tokens": unknown,
        CACHE_BREAKDOWN_STATUS_KEY: status,
    }


def merge_cache_usage(*sources) -> dict:
    """Sum cache usage across calls, keeping the accounting invariant.

    ``known_5m + known_1h + unknown == aggregate`` holds for the sum because
    it holds for every part. The status is re-derived from the summed
    counters — a call with complete detail merged with one that had none is
    honestly ``partial`` — except that ``inconsistent`` is sticky: an
    accounting warning raised on any component call must stay visible in the
    total, and no counter can reconstruct it.
    """
    total = empty_cache_usage()
    saw_inconsistent = False
    for source in sources:
        part = cache_usage_from(source)
        for key in CACHE_USAGE_TOKEN_KEYS:
            total[key] += part[key]
        if part[CACHE_BREAKDOWN_STATUS_KEY] == CACHE_BREAKDOWN_INCONSISTENT:
            saw_inconsistent = True
    total[CACHE_BREAKDOWN_STATUS_KEY] = derive_cache_breakdown_status(
        aggregate=total["cache_creation_input_tokens"],
        known_5m=total["cache_creation_5m_input_tokens"],
        known_1h=total["cache_creation_1h_input_tokens"],
        unknown=total["cache_creation_unknown_input_tokens"],
    )
    if saw_inconsistent and total["cache_creation_input_tokens"]:
        total[CACHE_BREAKDOWN_STATUS_KEY] = CACHE_BREAKDOWN_INCONSISTENT
    return total


def apply_cache_usage(target, usage) -> dict:
    """Stamp an extracted cache-usage dict onto a carrier object.

    ``usage`` is either a raw SDK/dict usage block or an already-extracted
    dict. One setter for every carrier keeps a new counter from being wired
    into some assignment sites and forgotten at others — the failure mode that
    silently under-reports one phase's spend. Returns the dict it applied.
    """
    if isinstance(usage, dict) and CACHE_BREAKDOWN_STATUS_KEY in usage:
        extracted = dict(usage)
    else:
        extracted = extract_cache_usage(usage)
    for key, value in extracted.items():
        setattr(target, key, value)
    return extracted


def cache_pricing_kwargs(source) -> dict:
    """The cache keyword arguments :func:`estimate_cost_breakdown` accepts.

    Drops the status (pricing reads counters, not labels) and keeps every
    token key, so a caller can splat this without the aggregate and its own
    components ever being charged twice.
    """
    usage = cache_usage_from(source)
    return {key: usage[key] for key in CACHE_PRICING_KEYS}


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

ENV_GOVERNING_BASIS_CONTEXT = "SPEC_CRITIC_GOVERNING_BASIS_CONTEXT"
ENV_CACHE_DIAGNOSTICS = "SPEC_CRITIC_CACHE_DIAGNOSTICS"
CACHE_DIAGNOSTICS_BETA = "cache-diagnosis-2026-04-07"

# Mirrors the disable-token convention used by the tracing / cache modules.
_DISABLE_TOKENS = frozenset({"0", "false", "no", "off"})


def governing_basis_context_enabled() -> bool:
    """Whether the verifier prompt renders the run's governing basis. Default OFF.

    This is the **researched-context expansion** half of the edition-authority
    work (CLAUDE.md, "Researched-context expansion"), and it stays gated for a
    reason worth restating: putting researched adoption claims into a
    verification prompt is a change in what the verifier is asked, and that
    change must be measured against the data-center applicability set
    (``evals/dc_applicability.py``) —
    reporting incorrect confirmations and incorrect disputes separately —
    before it becomes the default.

    The **provenance-only correction** it builds on is NOT gated and is always
    active: pinned editions are never presented as authoritative on a
    location-aware module, whatever this flag says. Turning this flag off can
    therefore never restore the superseded authoritative wording.

    Opt in with ``SPEC_CRITIC_GOVERNING_BASIS_CONTEXT`` set to any truthy,
    non-disable value.
    """
    raw = os.environ.get(ENV_GOVERNING_BASIS_CONTEXT)
    if raw is None:
        return False
    val = raw.strip().lower()
    return val != "" and val not in _DISABLE_TOKENS


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


# ---------------------------------------------------------------------------
# Experiment EX-01: a review breakpoint after the shared Project Context
# ---------------------------------------------------------------------------
#
# Default OFF and NOT EVALUATED (plans/experiments/EX-01-project-context-
# caching.md). Every review request of one module in one run carries the same
# prefix up to the end of its ``<project_context>`` block: the same tools,
# system prompt, thinking and effort settings, and the same user-message head
# (the module's intro, code-basis line, reminders, and the context itself).
# Only the spec that follows differs. Today the explicit breakpoints stop at
# the tools and the system prompt, so that context is billed at the full input
# rate once per spec. With this switch on, the review user message is sent as
# two text blocks — the head, ending with the context block, carries a
# breakpoint; the spec, alerts, and closing task follow — so a later request
# can read the context from the cache instead.
#
# Whether that saves money is the open question, not a given: a request that
# misses pays a cache write (1.25x the input rate for five minutes, 2x for one
# hour) on the whole head, and Message Batches items are processed
# concurrently with cache hits on a best-effort basis. It pays only when the
# share of requests that write stays under 0.9 / 1.9 (about 47%) at the
# one-hour TTL, or 0.9 / 1.15 (about 78%) at five minutes, which only a live
# run can tell. Hence the switch, and hence it stays off.
#
# Values: unset, empty, or ``0`` / ``false`` / ``no`` / ``off`` — off (the
# request is byte-identical to a build without the switch). ``1h`` (or ``1`` /
# ``true`` / ``yes`` / ``on``) — a one-hour breakpoint, the TTL every other
# breakpoint the app sets uses. ``5m`` — a five-minute breakpoint (allowed after
# the one-hour ones: longer TTLs must come first). Anything else is off, with
# one warning: an experiment switch fails closed rather than guessing a TTL.

ENV_PROJECT_CONTEXT_CACHE = "SPEC_CRITIC_PROJECT_CONTEXT_CACHE"

_PROJECT_CONTEXT_CACHE_ONE_HOUR = frozenset({"1h", "1", "true", "yes", "on"})
_PROJECT_CONTEXT_CACHE_FIVE_MINUTES = frozenset({"5m"})
_WARNED_PROJECT_CONTEXT_CACHE_VALUES: set[str] = set()


def project_context_cache_control() -> dict | None:
    """The ``cache_control`` for the review's Project Context block, or ``None``.

    ``None`` (the default) means the review user message stays one string.
    Read at call time, so a test or an evaluation arm can switch it with the
    environment alone. See the section comment above for the values.
    """
    raw = os.environ.get(ENV_PROJECT_CONTEXT_CACHE)
    if raw is None:
        return None
    val = raw.strip().lower()
    if val == "" or val in _DISABLE_TOKENS:
        return None
    if val in _PROJECT_CONTEXT_CACHE_ONE_HOUR:
        return _cache_control_block()
    if val in _PROJECT_CONTEXT_CACHE_FIVE_MINUTES:
        return {"type": "ephemeral", "ttl": "5m"}
    if val not in _WARNED_PROJECT_CONTEXT_CACHE_VALUES:
        _WARNED_PROJECT_CONTEXT_CACHE_VALUES.add(val)
        _log.warning(
            "%s=%r is not a recognized value (use 1h or 5m); the Project Context "
            "breakpoint stays off.",
            ENV_PROJECT_CONTEXT_CACHE,
            raw,
        )
    return None


# ---------------------------------------------------------------------------
# Experiment EX-04: evidence validation and source reuse (both default off)
# ---------------------------------------------------------------------------
#
# Two switches for two independent decisions (plan EX-04). The decision record
# is ``plans/experiments/EX-04-evidence-validation-source-reuse.md``.
#
# ``SPEC_CRITIC_EVIDENCE_VALIDATION`` — ``observe`` (or ``1`` / ``true`` /
# ``yes`` / ``on``) records an assessment of each conclusive verdict's evidence
# (``verification.evidence_validation``) beside it: diagnostics, the trace, and
# the result's runtime-only ``evidence_assessment``. It never changes a
# verdict, grounding, cache eligibility, a cache key, or a report. There is no
# enforcing value: ``enforce`` is refused like any unknown value, because
# nothing has measured the rules it would enforce.
#
# ``SPEC_CRITIC_SOURCE_REUSE`` — the within-run source-reuse prototype
# (``verification.source_reuse``). ``shadow`` records, for every fresh
# verification, what the run's store would have supplied and what the
# verification retrieved, and supplies nothing: requests stay byte-identical,
# on either transport. ``supply`` also hands a later verification the passages
# the API cited while verifying an earlier finding with the same claim
# context; real-time transport only (a batch run falls back to ``shadow``,
# with one warning). Off keeps every request and result byte-identical.
#
# For both: unset, empty, or ``0`` / ``false`` / ``no`` / ``off`` is off, and
# any other value is off with one warning — an experiment switch fails closed.

ENV_EVIDENCE_VALIDATION = "SPEC_CRITIC_EVIDENCE_VALIDATION"
ENV_SOURCE_REUSE = "SPEC_CRITIC_SOURCE_REUSE"

EVIDENCE_VALIDATION_OBSERVE = "observe"
SOURCE_REUSE_SHADOW = "shadow"
SOURCE_REUSE_SUPPLY = "supply"
_EVIDENCE_VALIDATION_VALUES = {
    "observe": EVIDENCE_VALIDATION_OBSERVE,
    "1": EVIDENCE_VALIDATION_OBSERVE,
    "true": EVIDENCE_VALIDATION_OBSERVE,
    "yes": EVIDENCE_VALIDATION_OBSERVE,
    "on": EVIDENCE_VALIDATION_OBSERVE,
}
# No truthy shorthand for reuse: ``shadow`` and ``supply`` differ in whether
# requests change, so the value must say which.
_SOURCE_REUSE_VALUES = {"shadow": SOURCE_REUSE_SHADOW, "supply": SOURCE_REUSE_SUPPLY}
_WARNED_EX04_VALUES: set[tuple[str, str]] = set()


def _ex04_switch(name: str, accepted: dict[str, str], usage: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    val = raw.strip().lower()
    if val == "" or val in _DISABLE_TOKENS:
        return None
    if val in accepted:
        return accepted[val]
    if (name, val) not in _WARNED_EX04_VALUES:
        _WARNED_EX04_VALUES.add((name, val))
        _log.warning("%s=%r is not a recognized value (%s); it stays off.", name, raw, usage)
    return None


def evidence_validation_mode() -> str | None:
    """``"observe"`` when evidence validation is switched on, else ``None``.

    Read at call time. Observation is the only mode; see the section comment.
    """
    return _ex04_switch(ENV_EVIDENCE_VALIDATION, _EVIDENCE_VALIDATION_VALUES, "use observe")


def source_reuse_mode() -> str | None:
    """``"shadow"`` / ``"supply"`` for the source-reuse experiment, else ``None``."""
    return _ex04_switch(ENV_SOURCE_REUSE, _SOURCE_REUSE_VALUES, "use shadow or supply")


# ---------------------------------------------------------------------------
# Experiment EX-05: reuse requirements research across runs (default off)
# ---------------------------------------------------------------------------
#
# ``SPEC_CRITIC_RESEARCH_CACHE`` — the research cache
# (``research.research_cache``; decision record
# ``plans/experiments/EX-05-research-reuse.md``). ``reuse`` looks up a stored,
# completed requirements profile for exactly this run's research requests and
# uses it instead of researching when it is young enough and names no date
# that has passed; on a miss it researches and stores a completed result.
# ``refresh`` researches again whatever is stored and replaces it — the
# deliberate refresh path. No truthy shorthand: the two differ in whether a
# stored profile may be used. Unset, empty, or ``0`` / ``false`` / ``no`` /
# ``off`` is off, and off is byte-identical (the cache file is never read or
# written); any other value is off with one warning.
#
# ``SPEC_CRITIC_RESEARCH_CACHE_MAX_AGE_DAYS`` — the oldest profile a lookup
# may reuse, in whole days: 1 to 90, default 30. Anything else (``0``
# included — "never expire" is exactly what research must not do) is the
# default, with one warning.

ENV_RESEARCH_CACHE = "SPEC_CRITIC_RESEARCH_CACHE"
ENV_RESEARCH_CACHE_MAX_AGE_DAYS = "SPEC_CRITIC_RESEARCH_CACHE_MAX_AGE_DAYS"

RESEARCH_CACHE_REUSE = "reuse"
RESEARCH_CACHE_REFRESH = "refresh"
_RESEARCH_CACHE_VALUES = {
    "reuse": RESEARCH_CACHE_REUSE,
    "refresh": RESEARCH_CACHE_REFRESH,
}
RESEARCH_CACHE_DEFAULT_MAX_AGE_DAYS = 30
RESEARCH_CACHE_MAX_AGE_LIMIT_DAYS = 90
_WARNED_RESEARCH_CACHE_AGES: set[str] = set()


def research_cache_mode() -> str | None:
    """``"reuse"`` / ``"refresh"`` for the research-cache experiment, else ``None``.

    Read at call time. Uses the same fail-closed parsing as the EX-04 switches.
    """
    return _ex04_switch(ENV_RESEARCH_CACHE, _RESEARCH_CACHE_VALUES, "use reuse or refresh")


def research_cache_max_age_days() -> int:
    """The research cache's age limit in days (see the section comment)."""
    raw = os.environ.get(ENV_RESEARCH_CACHE_MAX_AGE_DAYS)
    if raw is None or not raw.strip():
        return RESEARCH_CACHE_DEFAULT_MAX_AGE_DAYS
    text = raw.strip()
    try:
        value = int(text)
    except ValueError:
        value = None
    if value is not None and 1 <= value <= RESEARCH_CACHE_MAX_AGE_LIMIT_DAYS:
        return value
    if text not in _WARNED_RESEARCH_CACHE_AGES:
        _WARNED_RESEARCH_CACHE_AGES.add(text)
        _log.warning(
            "%s=%r is not a whole number of days from 1 to %d; using %d.",
            ENV_RESEARCH_CACHE_MAX_AGE_DAYS,
            raw,
            RESEARCH_CACHE_MAX_AGE_LIMIT_DAYS,
            RESEARCH_CACHE_DEFAULT_MAX_AGE_DAYS,
        )
    return RESEARCH_CACHE_DEFAULT_MAX_AGE_DAYS
