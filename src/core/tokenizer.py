"""
Token counting and limit management for Claude API calls.

Uses tiktoken with cl100k_base for approximate preflight estimates.
These counts are used for guardrails, not exact billing.

Token limits (v2.3.0):
    - Claude Opus 4.8 context window: 1,000,000 tokens
    - Opus 4.8 / Sonnet 5 max output: 128,000 tokens
    - Sonnet 4.6 max output: 64,000 tokens
    - Per-spec recommended input limit: 500,000 tokens
      (practical limit — individual specs are reviewed one at a time)
    - Cross-check recommended input limit: ~822,000 tokens
      (1,000,000 context - 128,000 output reserve - 50,000 overhead)

The per-spec limit (RECOMMENDED_MAX) is intentionally conservative
relative to the 1M context window. Per-spec review calls send a single
spec at a time, and the token gauge in the GUI displays the largest
spec's call size against this limit.

The cross-check limit (CROSS_CHECK_RECOMMENDED_MAX) is much higher
because the cross-checker sends ALL spec content in a single call.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import tiktoken

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model limits
# ---------------------------------------------------------------------------

# Claude Opus 4.8 context window (1M tokens, no beta header required).
MAX_CONTEXT_TOKENS = 1_000_000


# ---------------------------------------------------------------------------
# Per-spec review limits (used by GUI token gauge and per-spec pipeline)
# ---------------------------------------------------------------------------

# Practical per-call input limit for per-spec reviews.
# Individual specs are reviewed one at a time — this is the budget for a
# single (system prompt + project context + spec content) API call.
# Conservative relative to the 1M window and intended as a practical guardrail.
RECOMMENDED_MAX = 500_000

# Hard cap on the Project Context block. The context is sent on every per-spec
# review call, every cross-check call, and every verification call, so it
# multiplies cost quickly. 100K tokens leaves ~400K of the per-spec budget for
# the spec itself.
PROJECT_CONTEXT_MAX_TOKENS = 100_000


# ---------------------------------------------------------------------------
# Cross-check limits (v2.2.0)
# ---------------------------------------------------------------------------

# Cross-check uses Sonnet 5 with full spec content and adaptive thinking.
# With thinking enabled, thinking tokens + text output share the max_tokens budget.
# We keep a 128K output reserve (matches the api_config cross-check cap before
# the per-model clamp) so the input budget stays stable across model changes.
# Budget: 1M context - 128K output reserve - 50K overhead = 822K
CROSS_CHECK_OVERHEAD = 50_000
CROSS_CHECK_OUTPUT_BUDGET = 128_000
CROSS_CHECK_RECOMMENDED_MAX = (
    MAX_CONTEXT_TOKENS - CROSS_CHECK_OUTPUT_BUDGET - CROSS_CHECK_OVERHEAD
)


def exceeds_per_call_limit(spec_tokens: int, overhead_tokens: int) -> bool:
    """Check if a single spec would exceed the per-call token limit.

    Backward-compatible wrapper: no safety factor applied. New code that
    needs model-aware behavior should call
    :func:`exceeds_per_call_limit_for_model` instead.
    """
    return (overhead_tokens + spec_tokens) > RECOMMENDED_MAX


# ---------------------------------------------------------------------------
# Model-specific safety multipliers for the local cl100k_base estimate
# ---------------------------------------------------------------------------
#
# cl100k_base is OpenAI's tokenizer and does not exactly match Claude's
# tokenization. The undercount is usually modest for English prose
# (≤10%) but can be larger for structured spec text full of section
# numbers, table cells, and unicode punctuation. Without a safety factor
# the local estimate looks reassuring even when the real Claude count
# would breach the per-call budget — the goal is that local tokenizer
# estimates no longer create false confidence.
#
# The multipliers below are intentionally conservative. They are only
# consulted on the fallback path when the Anthropic ``count_tokens``
# endpoint is unavailable; once we have an exact count, that becomes
# the authoritative gate (directive 3).
_DEFAULT_LOCAL_SAFETY_FACTOR = 1.20  # unknown models — widest margin
_LOCAL_SAFETY_FACTORS: dict[str, float] = {
    # Opus / Sonnet 4.6 share Claude's main tokenizer; the cl100k_base
    # undercount is small but non-zero. Opus 5 stays at the Opus 4.8 factor:
    # both are on the Opus 4.7-family tokenizer (the models overview quotes an
    # identical "~555k words / ~2.5M unicode characters" 1M window for the two,
    # and the Opus 4.8 → Opus 5 migration guide carries no tokenizer
    # re-baseline step) — do NOT copy Sonnet 5's 1.45 here.
    "claude-opus-5": 1.10,
    "claude-opus-4-8": 1.10,
    "claude-sonnet-4-6": 1.10,
    # Sonnet 5 uses a NEW tokenizer that produces ~30% more tokens than the
    # 4.6-family tokenizer for the same text (per Anthropic's Sonnet 5
    # migration guide). Compounding the family's 1.10 cl100k pad with that
    # shift gives ~1.43; round up so the fallback gate never gains false
    # confidence from a pre-migration multiplier.
    "claude-sonnet-5": 1.45,
    # Haiku 4.5 tokenization tends to undercount cl100k a bit more on
    # structured construction-spec text in practice. Pad more.
    "claude-haiku-4-5": 1.15,
}


def local_estimate_safety_factor(model: str | None) -> float:
    """Return the cl100k→Claude safety multiplier for ``model``.

    The factor is a conservative multiplier ≥ 1.0 applied to the local
    cl100k_base count whenever it is used as a budget gate. Unknown
    models fall back to ``_DEFAULT_LOCAL_SAFETY_FACTOR`` (the widest
    margin) so a future model never silently sails through a budget
    check that would have been blocked under a known model.

    The ``≥ 1.0`` floor is *enforced*, not merely assumed: a sub-1.0 entry
    slipping into ``_LOCAL_SAFETY_FACTORS`` (a typo, or a misguided attempt
    to trim the margin) would turn the safety pad into a *danger pad* — it
    would shrink the estimate below the raw local count, undercount the
    Claude token total, and let an over-budget spec sail through the
    fallback gate. Clamping here honors the contract for every caller, not
    just :func:`safe_local_estimate`.
    """
    factor = _LOCAL_SAFETY_FACTORS.get(model or "", _DEFAULT_LOCAL_SAFETY_FACTOR)
    return max(1.0, factor)


def safe_local_estimate(local_tokens: int, *, model: str | None) -> int:
    """Return ``local_tokens`` padded by the model-specific safety factor."""
    factor = local_estimate_safety_factor(model)
    # Round up — the factor is a safety margin, not a midpoint estimate.
    return math.ceil(local_tokens * factor)


def exceeds_per_call_limit_for_model(
    spec_tokens: int,
    overhead_tokens: int,
    *,
    model: str | None,
) -> bool:
    """Model-aware version of :func:`exceeds_per_call_limit`.

    Applies the model-specific safety factor to ``spec_tokens + overhead``
    before comparing against ``RECOMMENDED_MAX``. Use this when the local
    cl100k_base count is the only signal available (e.g. the API preflight
    failed or was disabled). When an exact Anthropic count is available,
    bypass this helper and compare the exact count directly to
    ``RECOMMENDED_MAX`` — the exact number is authoritative (directive 3).
    """
    padded = safe_local_estimate(overhead_tokens + spec_tokens, model=model)
    return padded > RECOMMENDED_MAX


# ---------------------------------------------------------------------------
# Encoder loading — memoized, and honest about where the rank file comes from
# ---------------------------------------------------------------------------
#
# The tiktoken wheel does NOT ship the cl100k_base BPE rank file. On first use
# ``tiktoken.get_encoding`` looks for a cached copy in the directory named by
# ``TIKTOKEN_CACHE_DIR`` (then ``DATA_GYM_CACHE_DIR``, then
# ``<tempdir>/data-gym-cache``) and, when the file is absent, downloads it from
# a public Azure blob host. A workstation that allows api.anthropic.com but
# blocks that host therefore fails at the very first ``count_tokens`` call —
# and it used to fail with a bare connection error that named neither
# tiktoken, nor the cache directory, nor the download. The Windows build
# bundles the rank file and points ``TIKTOKEN_CACHE_DIR`` at it before any
# ``src`` import (``packaging/windows/app_entry.py``;
# ``packaging/windows/bundle_assets.py`` decides what to bundle and verifies
# it at build time).
#
# The constants below are this repo's single description of the file tiktoken
# expects — shared by the build helper (what to bundle, how to verify it), the
# frozen app's ``--selfcheck`` probe, and the actionable error raised here.
# They mirror ``tiktoken_ext/openai_public.py::cl100k_base`` for the pinned
# tiktoken release; ``tests/test_tokenizer_encoder.py`` fails if that pin
# drifts from these values.

ENCODING_NAME = "cl100k_base"
CL100K_BASE_BLOB_URL = (
    "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
)
CL100K_BASE_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
TIKTOKEN_CACHE_DIR_ENV = "TIKTOKEN_CACHE_DIR"
_TIKTOKEN_LEGACY_CACHE_DIR_ENV = "DATA_GYM_CACHE_DIR"
_TIKTOKEN_DEFAULT_CACHE_SUBDIR = "data-gym-cache"


def cl100k_cache_filename() -> str:
    """The file name tiktoken caches the cl100k_base rank file under.

    tiktoken keys its cache by ``sha1(<download URL>)`` — no extension, no
    encoding name — so the bundled file and the self-check must use the same
    derivation rather than a human-readable name.
    """
    return hashlib.sha1(CL100K_BASE_BLOB_URL.encode("utf-8")).hexdigest()


def effective_tiktoken_cache_dir(environ: Mapping[str, str] | None = None) -> str:
    """The directory tiktoken will look in for (and write) its rank-file cache.

    Mirrors the resolution order in ``tiktoken.load.read_file_cached``:
    ``TIKTOKEN_CACHE_DIR`` → ``DATA_GYM_CACHE_DIR`` → ``<tempdir>/data-gym-cache``.
    An *empty* ``TIKTOKEN_CACHE_DIR`` disables caching inside tiktoken (every
    load fetches); it is returned as-is so a report can show it verbatim.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    if TIKTOKEN_CACHE_DIR_ENV in env:
        return env[TIKTOKEN_CACHE_DIR_ENV]
    if _TIKTOKEN_LEGACY_CACHE_DIR_ENV in env:
        return env[_TIKTOKEN_LEGACY_CACHE_DIR_ENV]
    return os.path.join(tempfile.gettempdir(), _TIKTOKEN_DEFAULT_CACHE_SUBDIR)


@dataclass(frozen=True)
class EncoderCacheStatus:
    """Where tiktoken will look for the cl100k_base rank file, and whether it is there."""

    cache_dir: str
    rank_file: str
    rank_file_present: bool


def encoder_cache_status(environ: Mapping[str, str] | None = None) -> EncoderCacheStatus:
    """Snapshot the cache location + rank-file presence WITHOUT loading anything.

    Sampled *before* a load, this distinguishes "read the local file" from
    "downloaded it" — the frozen app's ``--selfcheck`` relies on that because
    the CI runner has network access, so a post-load check could not tell the
    two apart.
    """
    cache_dir = effective_tiktoken_cache_dir(environ)
    rank_file = os.path.join(cache_dir, cl100k_cache_filename()) if cache_dir else ""
    present = bool(rank_file) and os.path.isfile(rank_file)
    return EncoderCacheStatus(
        cache_dir=cache_dir, rank_file=rank_file, rank_file_present=present
    )


class EncoderLoadError(RuntimeError):
    """The cl100k_base encoder could not be loaded.

    Raised by :func:`get_encoder` (and therefore :func:`count_tokens`) in
    place of tiktoken's bare connection / hash error, with a message that
    names the cache directory in effect and explains the network fetch. A
    plain ``RuntimeError`` subclass, so every existing ``except Exception``
    around ``count_tokens`` keeps catching it; no caller catches a narrower
    class.
    """


_ENCODER: Any = None
_ENCODER_LOCK = threading.Lock()


def _encoder_load_message(exc: BaseException, status: EncoderCacheStatus) -> str:
    if status.cache_dir:
        state = "present" if status.rank_file_present else "absent"
        location = (
            f"Cache directory in effect: {status.cache_dir!r} "
            f"(rank file {status.rank_file!r} is {state})"
        )
    else:
        location = (
            f"{TIKTOKEN_CACHE_DIR_ENV} is set to an empty string, which disables "
            f"tiktoken's cache so every load downloads"
        )
    return (
        f"Could not load the {ENCODING_NAME} tokenizer used for local token "
        f"estimates ({type(exc).__name__}: {exc}). tiktoken does not ship the "
        f"BPE rank file: it reads a cached copy from the directory named by "
        f"{TIKTOKEN_CACHE_DIR_ENV} and, when the file is absent, downloads it "
        f"from {CL100K_BASE_BLOB_URL} — a host commonly blocked on networks "
        f"that allow api.anthropic.com. {location}. Fix: put the rank file in "
        f"that directory, or set {TIKTOKEN_CACHE_DIR_ENV} to a directory that "
        f"already contains it (the Windows installer bundles one and sets the "
        f"variable itself)."
    )


def get_encoder():
    """Return the process-wide cl100k_base encoder, loading it at most once.

    Memoized per process (double-checked under a lock) so the registry lookup
    and the first-use rank-file load happen once rather than on every
    ``count_tokens`` call — the GUI counts every selected file plus the system
    prompt on each browse. A *failed* load is not memoized: the next call
    retries, so fixing ``TIKTOKEN_CACHE_DIR`` (or the network) takes effect
    without a restart.

    Raises:
        EncoderLoadError: the rank file was neither cached nor fetchable, or
            was rejected as corrupt. Chained from tiktoken's original error;
            the message names the cache directory in effect and the download
            tiktoken attempted.
    """
    global _ENCODER
    encoder = _ENCODER
    if encoder is not None:
        return encoder
    with _ENCODER_LOCK:
        if _ENCODER is None:
            status = encoder_cache_status()
            try:
                _ENCODER = tiktoken.get_encoding(ENCODING_NAME)
            except Exception as exc:
                message = _encoder_load_message(exc, status)
                _log.error("%s", message)
                raise EncoderLoadError(message) from exc
        return _ENCODER


def _reset_encoder_for_tests() -> None:
    """Drop the memoized encoder (test seam; never called by app code)."""
    global _ENCODER
    with _ENCODER_LOCK:
        _ENCODER = None


def count_tokens(text: str) -> int:
    """Count tokens in a text string (local cl100k_base estimate).

    Raises :class:`EncoderLoadError` (an ``Exception`` subclass) when the
    encoder cannot be loaded — see :func:`get_encoder`.
    """
    encoder = get_encoder()
    return len(encoder.encode(text))


# ---------------------------------------------------------------------------
# Image / vision token estimation
# ---------------------------------------------------------------------------
#
# Claude bills an image at approximately ``width * height / 750`` tokens (per
# the vision docs), after any resize down to the model's native resolution, and
# clamped to a per-model token cap. The published cost tables match
# ``ceil(w*h/750)`` with no extra padding, so we mirror that exactly:
#
#   * High-resolution tier — every model in ``api_config.HIRES_VISION_MODELS``
#     (the single source of truth for which models get it): up to 4784 tokens,
#     long edge <= 2576 px.
#   * Every other model, including unknown ids: up to 1568 tokens, long edge
#     <= 1568 px.
#
# These are local *estimates* for budgeting (mirroring the documented formula);
# the authoritative number is still Anthropic's ``count_tokens`` endpoint, which
# accepts image/document blocks like any other content.

_IMAGE_TOKEN_DIVISOR = 750

_IMAGE_TOKEN_CAP_HIRES = 4784      # models in api_config.HIRES_VISION_MODELS
_IMAGE_LONG_EDGE_HIRES = 2576
_IMAGE_TOKEN_CAP_DEFAULT = 1568    # every other model / unknown ids
_IMAGE_LONG_EDGE_DEFAULT = 1568


def _image_caps_for_model(model: str | None) -> tuple[int, int]:
    """Return ``(token_cap, long_edge_cap_px)`` for ``model``.

    Reads the high-resolution vision tier from the api_config whitelist
    (``HIRES_VISION_MODELS``) so the capability source of truth stays single —
    membership is decided there, never restated here. Imported lazily to avoid
    any import-order coupling at module load.
    """
    try:
        from .api_config import HIRES_VISION_MODELS

        if model in HIRES_VISION_MODELS:
            return _IMAGE_TOKEN_CAP_HIRES, _IMAGE_LONG_EDGE_HIRES
    except Exception:  # pragma: no cover - defensive; fall back to safe default
        pass
    return _IMAGE_TOKEN_CAP_DEFAULT, _IMAGE_LONG_EDGE_DEFAULT


def estimate_image_tokens(width_px: int, height_px: int, *, model: str | None) -> int:
    """Estimate the billed token cost of one image of ``width_px x height_px``.

    Mirrors the documented vision pricing: resize down so the long edge fits the
    model's native resolution (preserving aspect ratio), then ``ceil(w*h/750)``,
    clamped to the per-model token cap. Returns an integer >= 0.
    """
    if width_px <= 0 or height_px <= 0:
        return 0
    token_cap, long_edge_cap = _image_caps_for_model(model)
    w = float(width_px)
    h = float(height_px)
    longest = max(w, h)
    if longest > long_edge_cap:
        scale = long_edge_cap / longest
        w *= scale
        h *= scale
    tokens = math.ceil((w * h) / _IMAGE_TOKEN_DIVISOR)
    return min(token_cap, tokens)


def estimate_image_tokens_total(
    sizes: list[tuple[int, int]], *, model: str | None
) -> int:
    """Sum :func:`estimate_image_tokens` over a list of ``(width, height)`` sizes."""
    return sum(estimate_image_tokens(w, h, model=model) for w, h in sizes)


def count_tokens_via_api(
    *,
    model: str,
    system: Any,
    messages: list[dict],
    tools: Optional[list[dict]] = None,
    client: Any = None,
) -> Optional[int]:
    """Exact token count via Anthropic's count_tokens endpoint.

    Returns the input-token total for the given request shape, or ``None`` on
    failure (network error, missing API key, SDK version mismatch). Callers
    should treat ``None`` as "preflight unavailable" and fall back to the
    local estimate rather than blocking submission.

    Plan section 6.3: keep the local estimate for UI responsiveness, use this
    helper before batch submission when exact routing/guardrail decisions
    matter.
    """
    if client is None:
        try:
            from ..review.reviewer import _get_client
            client = _get_client()
        except Exception as exc:  # pragma: no cover - exercised via tests
            _log.warning("count_tokens_via_api: no client available (%s)", exc)
            return None
    try:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if system is not None:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        result = client.messages.count_tokens(**kwargs)
        # MessageTokensCount has an input_tokens attribute.
        return int(getattr(result, "input_tokens", 0) or 0)
    except Exception as exc:
        _log.warning("count_tokens_via_api failed: %s", exc)
        return None
