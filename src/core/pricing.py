"""Model pricing + request cost estimation (USD).

A small, dependency-free pricing table so the app can show a spend estimate
before launching an expensive run (e.g. a batch review) and price a finished
run's telemetry afterwards (``orchestration.diagnostics``). Rates are USD per
million tokens, current as of 2026-06. Image/vision input is billed as
ordinary input tokens, so no separate image rate is needed; the Batch API
bills token costs at 50% of standard, exposed via the ``batch=`` flag.

Beyond plain input/output tokens, two more line items matter for this app:

- **Prompt-cache writes and reads.** Every high-value phase sends
  ``cache_control`` with the 1-hour TTL (``api_config.cache_policy_for``). A
  1-hour cache write bills at :data:`CACHE_WRITE_1H_MULTIPLIER` (2×) the
  model's base *input* rate and a cache read at :data:`CACHE_READ_MULTIPLIER`
  (0.1×). Both are token costs, so both take the batch discount exactly like
  uncached input tokens.
- **Web searches.** Verification runs up to eight ``web_search`` calls per
  finding at :data:`WEB_SEARCH_USD_PER_1000` ($10 per 1,000 searches). The
  Batches API charges searches at the same rate, so the batch discount is
  applied to token costs only — never to searches.

**Rates drift — verify against the published pricing before relying on a figure.**
Unknown model ids return ``None`` from :func:`price_for` /
:func:`estimate_request_cost` / :func:`estimate_cost_breakdown` (the caller
shows scale without a dollar figure rather than guessing a wrong number).
"""
from __future__ import annotations

from dataclasses import dataclass

# Batch API bills token costs at half of standard, per Anthropic's published
# pricing. Server-tool usage (web searches) is NOT discounted.
BATCH_DISCOUNT = 0.5

# Prompt-cache multipliers, applied to the model's base INPUT rate.
#
# The app declares a 1-hour TTL on every breakpoint it sets, but that does not
# make every write a 1-hour write: server tools insert their own 5-minute
# breakpoint after tool results when the request already uses caching. Those
# bill at 1.25× and concentrate in verification, the phase that runs the most
# web searches — so pricing every write at 2× overstated that phase.
#
# ``CACHE_WRITE_UNKNOWN_MULTIPLIER`` is deliberately the 1-hour rate: when the
# provider gives no per-TTL detail, the conservative estimate is retained so
# legacy figures stay stable, and the uncertainty is made visible through the
# unknown-token count rather than hidden in a number that looks measured.
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
CACHE_WRITE_UNKNOWN_MULTIPLIER = CACHE_WRITE_1H_MULTIPLIER
CACHE_READ_MULTIPLIER = 0.1

# Web search is billed per request: $10 per 1,000 searches, identical on the
# standard and Batches APIs (only tokens carry the batch discount).
WEB_SEARCH_USD_PER_1000 = 10.0


@dataclass(frozen=True)
class ModelPrice:
    """USD per **million** tokens."""

    input_per_mtok: float
    output_per_mtok: float
    label: str  # human-friendly name for dialogs


# Keyed by the bare model id. A dated/fast/-suffixed variant resolves via the
# startswith fallback in ``price_for`` (e.g. "claude-haiku-4-5-20251001").
MODEL_PRICING: dict[str, ModelPrice] = {
    # Opus 5 is priced identically to Opus 4.8 ($5/$25) — the upgrade is
    # cost-neutral per token.
    "claude-opus-5": ModelPrice(5.00, 25.00, "Opus 5"),
    "claude-opus-4-8": ModelPrice(5.00, 25.00, "Opus 4.8"),
    "claude-opus-4-7": ModelPrice(5.00, 25.00, "Opus 4.7"),
    "claude-opus-4-6": ModelPrice(5.00, 25.00, "Opus 4.6"),
    # Sonnet 5's introductory pricing ($2/$10 per MTok) was made the
    # permanent standard price on 2026-08-10 — the previously scheduled
    # increase to $3/$15 on 2026-09-01 will not occur.
    "claude-sonnet-5": ModelPrice(2.00, 10.00, "Sonnet 5"),
    "claude-sonnet-4-6": ModelPrice(3.00, 15.00, "Sonnet 4.6"),
    "claude-haiku-4-5": ModelPrice(1.00, 5.00, "Haiku 4.5"),
}


def price_for(model: str) -> ModelPrice | None:
    """Resolve a model id to its price, tolerating dated/suffixed variants.

    Exact match first, then the longest known-prefix match so a variant like
    ``claude-haiku-4-5-20251001`` or ``claude-opus-4-8-fast`` still resolves.
    Returns ``None`` for an unrecognized id.
    """
    if not model:
        return None
    exact = MODEL_PRICING.get(model)
    if exact is not None:
        return exact
    # Only a *delimited* variant resolves to a base price — "claude-opus-4-8-fast"
    # or a dated "...-4-5-20251001", but NOT a different model whose id merely
    # starts with a known one (e.g. a future "claude-opus-4-80" must stay
    # unknown → None, not silently priced as 4.8). Longest match wins.
    best_key = ""
    for key in MODEL_PRICING:
        if model.startswith(key + "-") and len(key) > len(best_key):
            best_key = key
    return MODEL_PRICING[best_key] if best_key else None


def friendly_model_name(model: str) -> str:
    """Human-friendly label (``"Opus 4.8"``) for dialogs; falls back to the id."""
    price = price_for(model)
    return price.label if price is not None else model


@dataclass(frozen=True)
class CostBreakdown:
    """Estimated USD cost of a request, split into its billable line items.

    ``tokens`` is the uncached input + output token cost; ``cache_writes`` /
    ``cache_reads`` price the prompt-cache token counters off the input rate;
    ``web_searches`` is the per-request search charge. The three token line
    items carry the batch discount when requested; ``web_searches`` never
    does. :attr:`total` is their plain sum.
    """

    tokens: float
    cache_writes: float
    cache_reads: float
    web_searches: float

    @property
    def total(self) -> float:
        return self.tokens + self.cache_writes + self.cache_reads + self.web_searches

    def as_dict(self) -> dict[str, float]:
        """Line items plus ``total`` as a plain dict (for summaries / JSON)."""
        return {
            "tokens": self.tokens,
            "cache_writes": self.cache_writes,
            "cache_reads": self.cache_reads,
            "web_searches": self.web_searches,
            "total": self.total,
        }


def estimate_cost_breakdown(
    input_tokens: int,
    output_tokens: int,
    *,
    model: str,
    batch: bool = False,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    cache_creation_5m_input_tokens: int = 0,
    cache_creation_1h_input_tokens: int = 0,
    cache_creation_unknown_input_tokens: int | None = None,
    web_search_requests: int = 0,
) -> CostBreakdown | None:
    """Line-item cost estimate for a request, or ``None`` if the model is unknown.

    ``input_tokens`` is the *uncached* input count (the API's ``usage.
    input_tokens`` excludes cache reads/writes, which arrive as the two
    ``cache_*_input_tokens`` counters). Image/vision input counts as ordinary
    input tokens, so callers fold image tokens into ``input_tokens``.
    ``batch=True`` applies the 50% Batch discount to every token line item —
    uncached tokens, cache writes, and cache reads — but not to web searches,
    which the Batches API bills at the same per-request rate.

    **Cache writes are priced per TTL when the provider reports it.** Pass the
    per-TTL split alongside the aggregate: known 5-minute tokens bill at 1.25×
    the input rate, known 1-hour tokens at 2×, and anything the provider did
    not break down at the conservative 2×. The aggregate is **never** added to
    its own components — it is used only when no split is supplied
    (``cache_creation_unknown_input_tokens=None``), which keeps every existing
    caller's numbers byte-identical.
    """
    price = price_for(model)
    if price is None:
        return None
    factor = BATCH_DISCOUNT if batch else 1.0
    tokens = (
        (input_tokens / 1_000_000) * price.input_per_mtok
        + (output_tokens / 1_000_000) * price.output_per_mtok
    ) * factor
    # When ``cache_creation_unknown_input_tokens`` is not supplied, the
    # unknown amount is the aggregate MINUS whatever was broken out — never
    # the whole aggregate. With no components supplied (both default to 0)
    # the remainder *is* the aggregate, so every pre-breakdown caller prices
    # exactly as it did before; but a caller that supplies one component and
    # omits the unknown count must not be charged for that component twice,
    # once at its own rate and again inside the aggregate. Clamped at zero so
    # components exceeding the aggregate price only what was declared.
    unknown_tokens = (
        max(
            cache_creation_input_tokens
            - cache_creation_5m_input_tokens
            - cache_creation_1h_input_tokens,
            0,
        )
        if cache_creation_unknown_input_tokens is None
        else cache_creation_unknown_input_tokens
    )
    cache_writes = (
        (cache_creation_5m_input_tokens / 1_000_000)
        * price.input_per_mtok
        * CACHE_WRITE_5M_MULTIPLIER
        + (cache_creation_1h_input_tokens / 1_000_000)
        * price.input_per_mtok
        * CACHE_WRITE_1H_MULTIPLIER
        + (unknown_tokens / 1_000_000)
        * price.input_per_mtok
        * CACHE_WRITE_UNKNOWN_MULTIPLIER
    ) * factor
    cache_reads = (
        (cache_read_input_tokens / 1_000_000)
        * price.input_per_mtok
        * CACHE_READ_MULTIPLIER
        * factor
    )
    web_searches = (web_search_requests / 1_000) * WEB_SEARCH_USD_PER_1000
    return CostBreakdown(
        tokens=tokens,
        cache_writes=cache_writes,
        cache_reads=cache_reads,
        web_searches=web_searches,
    )


def estimate_request_cost(
    input_tokens: int,
    output_tokens: int,
    *,
    model: str,
    batch: bool = False,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    cache_creation_5m_input_tokens: int = 0,
    cache_creation_1h_input_tokens: int = 0,
    cache_creation_unknown_input_tokens: int | None = None,
    web_search_requests: int = 0,
) -> float | None:
    """Estimated USD cost of a request, or ``None`` if the model is unknown.

    The total of :func:`estimate_cost_breakdown` — see it for the line-item
    rules. The cache / search keywords default to zero, so a caller that only
    passes input and output tokens gets the same number it always did; leaving
    ``cache_creation_unknown_input_tokens`` at ``None`` prices the whole
    aggregate at the conservative one-hour write rate, which is what every
    pre-breakdown caller did.
    """
    breakdown = estimate_cost_breakdown(
        input_tokens,
        output_tokens,
        model=model,
        batch=batch,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_5m_input_tokens=cache_creation_5m_input_tokens,
        cache_creation_1h_input_tokens=cache_creation_1h_input_tokens,
        cache_creation_unknown_input_tokens=cache_creation_unknown_input_tokens,
        web_search_requests=web_search_requests,
    )
    return None if breakdown is None else breakdown.total
