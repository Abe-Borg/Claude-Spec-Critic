"""Chunk 10 — central cost estimator for Spec Critic runs.

Drives the user-facing "Estimated API Cost" display in the diagnostics
summary, the Word report, and the GUI run-completion view. Walks the
``DiagnosticEvent`` list rather than the per-phase rollup so the cost is
attributed to the exact (model, mode, web_search_requests) tuple of each
call — the rollup loses the per-event model when a phase sees both
Sonnet and Opus (verification + escalation).

The pricing table reflects Anthropic's published list rates for the
Claude 4.x model family at :data:`PRICING_AS_OF`. Operators should treat
the result as a conservative estimate, not exact billing — Anthropic's
invoiced amount can differ for caching tiers, regional pricing, or
prompt-cache writes shorter than the 1-hour TTL. Unknown models route
to :func:`model_pricing` returning ``None`` so the summary says
"cost unavailable" instead of guessing.

Pricing model:

- ``input_rate``  — USD per million standard input tokens.
- ``output_rate`` — USD per million output tokens.
- ``cache_write_1h_rate`` — USD per million tokens written into the
  1-hour cache (2x base input per Anthropic docs). Spec Critic's
  default cache TTL is 1h (see :func:`api_config._cache_control_block`),
  so this is the rate the run actually pays.
- ``cache_read_rate`` — USD per million tokens read from the cache
  (0.1x base input per Anthropic docs).

Discounts and add-ons:

- Anthropic Message Batches: 50% off input + output (cache writes /
  reads / web-search are not discounted by the batch tier).
- Web-search tool: $10 per 1,000 requests, regardless of model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .api_config import (
    MODEL_HAIKU_45,
    MODEL_OPUS_47,
    MODEL_SONNET_46,
)


# ---------------------------------------------------------------------------
# Pricing table (Anthropic list rates, USD per million tokens)
# ---------------------------------------------------------------------------

PRICING_AS_OF = "2026-05-01"
"""Snapshot date for the pricing table.

Surfaced in cost summaries so operators can see whether the numbers
might be stale. Treat as "informational" — Anthropic's billing system
is the source of truth; this estimator only approximates it for
planning purposes.
"""


@dataclass(frozen=True)
class ModelPricing:
    """Per-million-token pricing for one model.

    Rates are USD per 1M tokens. ``cache_write_1h_rate`` is 2x the base
    input rate (Anthropic's documented multiplier for the 1-hour cache
    tier); ``cache_read_rate`` is 0.1x the base input rate.
    """

    input_rate: float
    output_rate: float
    cache_write_1h_rate: float
    cache_read_rate: float


_PRICING: dict[str, ModelPricing] = {
    MODEL_OPUS_47: ModelPricing(
        input_rate=15.0,
        output_rate=75.0,
        cache_write_1h_rate=30.0,
        cache_read_rate=1.5,
    ),
    MODEL_SONNET_46: ModelPricing(
        input_rate=3.0,
        output_rate=15.0,
        cache_write_1h_rate=6.0,
        cache_read_rate=0.3,
    ),
    MODEL_HAIKU_45: ModelPricing(
        input_rate=1.0,
        output_rate=5.0,
        cache_write_1h_rate=2.0,
        cache_read_rate=0.1,
    ),
}


# Batch API: Anthropic discounts both input and output by 50%. Cache
# writes / reads and web-search tool usage are not discounted.
BATCH_DISCOUNT = 0.5

# Server-side web_search tool: $10 per 1,000 requests, flat across models.
WEB_SEARCH_PRICE_PER_1K = 10.0


def model_pricing(model: str) -> Optional[ModelPricing]:
    """Return the :class:`ModelPricing` record for ``model`` (or ``None``).

    ``None`` is the explicit "pricing unavailable" signal — callers
    should report "cost unavailable" rather than guessing with a
    default rate that could over- or under-charge.
    """
    if not model:
        return None
    return _PRICING.get(model)


# ---------------------------------------------------------------------------
# Per-event cost calculation
# ---------------------------------------------------------------------------


def _round_cents(value: float) -> float:
    """Round a USD value to 6 decimals for stable JSON output.

    Sub-cent precision matters because individual cache-read calls on
    Haiku come in at fractions of a cent; truncating to two decimals
    would silently zero them out.
    """
    return round(value, 6)


def estimate_event_cost(event_data: dict) -> Optional[dict]:
    """Compute USD cost for a single API-call event.

    Returns a breakdown dict, or ``None`` when the event's model is
    unknown (so the caller can record a "missing pricing" note). The
    breakdown shape mirrors the four pricing components plus the
    web-search add-on so the report and diagnostics can render
    line-item context without recomputing.

    ``event_data`` is the ``data`` dict from a :class:`DiagnosticEvent`
    that went through :meth:`DiagnosticsReport.record_api_call`.
    """
    if not event_data:
        return None
    model = str(event_data.get("model") or "").strip()
    pricing = model_pricing(model)
    if pricing is None:
        return None

    input_tokens = int(event_data.get("input_tokens", 0) or 0)
    output_tokens = int(event_data.get("output_tokens", 0) or 0)
    cache_create = int(event_data.get("cache_creation_input_tokens", 0) or 0)
    cache_read = int(event_data.get("cache_read_input_tokens", 0) or 0)
    web_searches = int(event_data.get("web_search_requests", 0) or 0)
    call_mode = str(event_data.get("call_mode") or "").lower()

    discount = BATCH_DISCOUNT if call_mode == "batch" else 1.0

    input_usd = (input_tokens / 1_000_000.0) * pricing.input_rate * discount
    output_usd = (output_tokens / 1_000_000.0) * pricing.output_rate * discount
    # Cache writes and reads bill at the same rate regardless of mode —
    # the batch discount applies to standard input/output only.
    cache_write_usd = (cache_create / 1_000_000.0) * pricing.cache_write_1h_rate
    cache_read_usd = (cache_read / 1_000_000.0) * pricing.cache_read_rate
    web_search_usd = (web_searches / 1_000.0) * WEB_SEARCH_PRICE_PER_1K

    total_usd = (
        input_usd + output_usd + cache_write_usd + cache_read_usd + web_search_usd
    )
    return {
        "model": model,
        "call_mode": call_mode or "realtime",
        "input_usd": _round_cents(input_usd),
        "output_usd": _round_cents(output_usd),
        "cache_write_usd": _round_cents(cache_write_usd),
        "cache_read_usd": _round_cents(cache_read_usd),
        "web_search_usd": _round_cents(web_search_usd),
        "total_usd": _round_cents(total_usd),
    }


# ---------------------------------------------------------------------------
# Run-level aggregation
# ---------------------------------------------------------------------------


def _empty_phase_bucket() -> dict:
    return {
        "calls": 0,
        "input_usd": 0.0,
        "output_usd": 0.0,
        "cache_write_usd": 0.0,
        "cache_read_usd": 0.0,
        "web_search_usd": 0.0,
        "total_usd": 0.0,
        "missing_pricing_calls": 0,
    }


def _empty_model_bucket() -> dict:
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "web_search_requests": 0,
        "total_usd": 0.0,
    }


def estimate_run_cost(events: Iterable[object]) -> dict:
    """Aggregate USD cost across every API-call event in a run.

    ``events`` is the list of :class:`diagnostics.DiagnosticEvent`
    records (or any objects with a ``phase`` attribute and a ``data``
    dict). The walk is read-only — the caller's events are not
    mutated.

    Returns a dict with the shape::

        {
            "available": bool,        # True iff at least one priced call ran
            "total_usd": float,
            "currency": "USD",
            "pricing_as_of": str,
            "by_phase": {phase_name: phase_bucket, ...},
            "by_model": {model: model_bucket, ...},
            "missing_pricing_models": [unknown_model_names...],
            "missing_pricing_calls": int,
            "notes": [disclaimer strings...],
        }

    ``available`` is ``False`` when zero priced calls were recorded
    (e.g. every call used an unknown model, or no API calls happened);
    the report renders "cost unavailable" in that case rather than a
    misleading zero.
    """
    by_phase: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    missing_models: set[str] = set()
    missing_calls = 0
    priced_calls = 0
    total_usd = 0.0
    web_search_total_requests = 0

    for e in events or ():
        data = getattr(e, "data", None)
        phase = getattr(e, "phase", "") or ""
        if not data:
            continue
        if not (
            data.get("api_call")
            or data.get("input_tokens")
            or data.get("output_tokens")
            or data.get("cache_creation_input_tokens")
            or data.get("cache_read_input_tokens")
            or data.get("web_search_requests")
            or data.get("model")
        ):
            continue
        web_search_total_requests += int(data.get("web_search_requests", 0) or 0)
        cost = estimate_event_cost(data)
        if cost is None:
            missing_calls += 1
            model = str(data.get("model") or "").strip()
            if model:
                missing_models.add(model)
            bucket = by_phase.setdefault(phase, _empty_phase_bucket())
            bucket["missing_pricing_calls"] += 1
            continue
        priced_calls += 1
        bucket = by_phase.setdefault(phase, _empty_phase_bucket())
        bucket["calls"] += 1
        bucket["input_usd"] = _round_cents(bucket["input_usd"] + cost["input_usd"])
        bucket["output_usd"] = _round_cents(bucket["output_usd"] + cost["output_usd"])
        bucket["cache_write_usd"] = _round_cents(
            bucket["cache_write_usd"] + cost["cache_write_usd"]
        )
        bucket["cache_read_usd"] = _round_cents(
            bucket["cache_read_usd"] + cost["cache_read_usd"]
        )
        bucket["web_search_usd"] = _round_cents(
            bucket["web_search_usd"] + cost["web_search_usd"]
        )
        bucket["total_usd"] = _round_cents(bucket["total_usd"] + cost["total_usd"])

        model = cost["model"]
        mb = by_model.setdefault(model, _empty_model_bucket())
        mb["calls"] += 1
        mb["input_tokens"] += int(data.get("input_tokens", 0) or 0)
        mb["output_tokens"] += int(data.get("output_tokens", 0) or 0)
        mb["cache_creation_input_tokens"] += int(
            data.get("cache_creation_input_tokens", 0) or 0
        )
        mb["cache_read_input_tokens"] += int(data.get("cache_read_input_tokens", 0) or 0)
        mb["web_search_requests"] += int(data.get("web_search_requests", 0) or 0)
        mb["total_usd"] = _round_cents(mb["total_usd"] + cost["total_usd"])

        total_usd += cost["total_usd"]

    notes: list[str] = []
    if priced_calls or missing_calls:
        notes.append(
            "Estimated API cost only — Anthropic's invoiced amount may differ "
            "for cache tiers, regional pricing, or prompt-cache TTL choices."
        )
        notes.append(f"Pricing table as of {PRICING_AS_OF}; may be stale.")
    if missing_models:
        notes.append(
            "Pricing unavailable for: " + ", ".join(sorted(missing_models))
        )

    return {
        "available": priced_calls > 0,
        "total_usd": _round_cents(total_usd),
        "currency": "USD",
        "pricing_as_of": PRICING_AS_OF,
        "by_phase": by_phase,
        "by_model": by_model,
        "missing_pricing_models": sorted(missing_models),
        "missing_pricing_calls": missing_calls,
        "priced_calls": priced_calls,
        "web_search_requests": web_search_total_requests,
        "notes": notes,
    }


def format_usd(value: float) -> str:
    """Render a USD amount with conservative precision.

    Values >= $1 use two decimals; smaller amounts show four decimals so
    a $0.0003 Haiku triage call does not display as "$0.00". Negative
    values are clamped to zero — they only happen if a future bug feeds
    negative tokens into the estimator.
    """
    v = max(0.0, float(value))
    if v >= 1.0:
        return f"${v:,.2f}"
    if v >= 0.01:
        return f"${v:.3f}"
    if v == 0.0:
        return "$0.00"
    return f"${v:.4f}"
