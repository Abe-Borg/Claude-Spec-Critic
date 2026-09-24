"""Model-aware request budgets (plan WP-08).

Every large request the app sends is sized before it is sent: the per-spec
review (the preflight gate, the batch extended-output threshold, and the
real-time oversize gate) and the two package-level passes, cross-check and
compliance, including every final chunk they split into. :class:`RequestBudget`
is the one answer all of them use, and it is always computed from the request
the call will actually send — the counting form is derived from the same
params (:func:`count_request_from_params`), never rebuilt in parallel.

Where a count comes from
------------------------
``count_source`` names the basis of ``count``, the number the fit decision
used:

* ``"api_estimate"`` — Anthropic's ``count_tokens`` endpoint, counted with the
  selected model's own tokenizer over the exact request shape (system,
  messages, tools, ``tool_choice``, ``thinking``). It is the provider's
  *estimate*: Anthropic documents that "the actual number of input tokens used
  when creating a message might differ by a small amount". It is never called
  exact, and a missing or malformed response never becomes a number
  (``tokenizer.validated_input_tokens``).
* ``"local_padded"`` — used only when no API estimate is available (the
  endpoint failed, returned something unusable, or counting is disabled): the
  local cl100k_base count of *every* counted part of the request — system
  text, message content, each tool definition, and
  :data:`TOOL_USE_SYSTEM_PROMPT_ALLOWANCE` for the system prompt the API adds
  when tools are present — multiplied by the model's safety factor
  (``tokenizer.local_estimate_safety_factor``). A conservative guess, never a
  measurement.
* ``"unavailable"`` — neither could be produced (no API estimate, and the
  local tokenizer could not load). Such a request never fits: nothing is sent
  without a size.

The ceiling
-----------
``input_ceiling = min(phase_limit, context_window - output_reserve -
safety_reserve)``. ``output_reserve`` is the request's real ``max_tokens``
(input and output share the context window), ``safety_reserve`` is
:data:`CONTEXT_SAFETY_RESERVE_FRACTION` of the window (room for the
estimate's documented error), and ``phase_limit`` is the practical limit a
phase already had (``RECOMMENDED_MAX`` for a review, 822,000 for the package
passes), kept wherever it is the more conservative of the two. So on a
1M-window model the phase limits still govern, and on a 200k-window model the
model's own ceiling does.

Count cache
-----------
API estimates are cached per digest of the complete counting form, the model
included (:func:`count_request_digest`), so an identical request shape is
counted once per process (the chunk planner and the pass it plans for, the
review preflight and the batch builder) and a count is never reused across
models or across any input that changes the shape. Only valid API estimates
are cached; padded local estimates are recomputed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from . import api_config as _api_config
from . import tokenizer as _tokenizer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COUNT_SOURCE_API = "api_estimate"
COUNT_SOURCE_LOCAL_PADDED = "local_padded"
COUNT_SOURCE_UNAVAILABLE = "unavailable"

# Reserve kept between a counted request (input + its max_tokens) and the
# model's context window, as a fraction of the window: 50,000 tokens on the
# 1M-window models (the same 50k the package passes' 822k limit already set
# aside), 10,000 on a 200k window. It absorbs the count estimate's documented
# error and the per-message framing a local count cannot see.
CONTEXT_SAFETY_RESERVE_FRACTION = 0.05

# Tokens of the system prompt the API adds whenever tools are present, for the
# local fallback only (the API estimate already includes it). Anthropic's
# tool-use pricing table tops out at 589 for the registered models (Sonnet 4.6
# with tool_choice any/tool; Opus 5 is 286/406, Sonnet 5 354/474, Haiku 4.5
# 496/588), so 600 covers every one of them before padding.
TOOL_USE_SYSTEM_PROMPT_ALLOWANCE = 600

# The request fields Anthropic's ``count_tokens`` endpoint counts. Everything
# else in a Messages request (``max_tokens``, ``output_config``,
# ``service_tier``, ``container``, a top-level ``cache_control``) changes what
# the model may produce or how it is billed, not the input size.
_COUNTED_FIELDS = ("model", "system", "messages", "tools", "tool_choice", "thinking")

_COUNT_CACHE_MAX_ENTRIES = 512


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InputCount:
    """The size of a request's input and where the number came from."""

    tokens: Optional[int]
    source: str
    local_tokens: Optional[int] = None
    padding_factor: Optional[float] = None
    unavailable_reason: Optional[str] = None


@dataclass(frozen=True)
class RequestBudget:
    """Whether one request fits the model that will run it.

    ``count`` is the number the decision used, ``count_source`` its basis
    (see the module docstring). ``local_tokens`` is the raw local count when
    one was taken (the fallback, or a caller that asked for it); it is never
    the decision's input on its own. ``unavailable_reason`` says why the API
    estimate was not used, whenever it was not.
    """

    model: str
    count: Optional[int]
    count_source: str
    context_window: int
    output_reserve: int
    safety_reserve: int
    phase_limit: Optional[int]
    input_ceiling: int
    fits: bool
    local_tokens: Optional[int] = None
    padding_factor: Optional[float] = None
    unavailable_reason: Optional[str] = None

    def size_text(self) -> str:
        """The input size in words a log line or a report can carry."""
        if self.count_source == COUNT_SOURCE_API and self.count is not None:
            return f"{self.count:,} tokens (API estimate for {self.model})"
        if self.count_source == COUNT_SOURCE_LOCAL_PADDED and self.count is not None:
            local = f"{self.local_tokens:,}" if self.local_tokens is not None else "?"
            factor = f"{self.padding_factor:.2f}" if self.padding_factor else "?"
            return (
                f"~{self.count:,} tokens (local estimate: {local} cl100k tokens "
                f"x {factor} padding for {self.model})"
            )
        return f"an unknown number of tokens for {self.model}"

    def describe(self) -> str:
        """``size_text`` plus the ceiling and, when relevant, why no API estimate."""
        text = f"{self.size_text()} against an input ceiling of {self.input_ceiling:,}"
        if self.unavailable_reason and self.count_source != COUNT_SOURCE_API:
            text += f"; no API estimate: {self.unavailable_reason}"
        return text


# ---------------------------------------------------------------------------
# Ceiling
# ---------------------------------------------------------------------------


def context_safety_reserve(context_window: int) -> int:
    """The reserve kept below ``context_window`` (rounded up)."""
    return math.ceil(max(0, int(context_window)) * CONTEXT_SAFETY_RESERVE_FRACTION)


def input_ceiling_for(
    model: str, *, output_reserve: int, phase_limit: Optional[int] = None
) -> tuple[int, int, int]:
    """``(context_window, safety_reserve, input_ceiling)`` for ``model``.

    The model's own ceiling is its context window minus ``output_reserve``
    (the request's ``max_tokens``) minus the safety reserve; ``phase_limit``,
    when given, caps it further. Never negative.
    """
    window = int(_api_config.model_capabilities(model).context_window)
    reserve = context_safety_reserve(window)
    model_ceiling = max(0, window - max(0, int(output_reserve)) - reserve)
    ceiling = model_ceiling if phase_limit is None else min(int(phase_limit), model_ceiling)
    return window, reserve, max(0, ceiling)


# ---------------------------------------------------------------------------
# Counting form
# ---------------------------------------------------------------------------


def _without_cache_control(value: Any) -> Any:
    """``value`` with ``cache_control`` removed from its top-level blocks.

    Cache breakpoints are pricing hints: the count ignores them, and leaving
    them out keeps a TTL policy change from changing a request's digest.
    """
    if isinstance(value, list):
        stripped = []
        for block in value:
            if isinstance(block, Mapping) and "cache_control" in block:
                block = {k: v for k, v in block.items() if k != "cache_control"}
            stripped.append(block)
        return stripped
    return value


def _messages_without_cache_control(messages: Any) -> Any:
    if not isinstance(messages, list):
        return messages
    out = []
    for message in messages:
        if isinstance(message, Mapping) and isinstance(message.get("content"), list):
            message = dict(message)
            message["content"] = _without_cache_control(message["content"])
        out.append(message)
    return out


def count_request_from_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """The ``count_tokens`` form of a Messages request's params.

    Keeps exactly the fields the endpoint counts (:data:`_COUNTED_FIELDS`)
    from the params the call will send, minus ``cache_control`` markers. The
    returned dict is a copy; mutating it never changes the request.
    """
    out: dict[str, Any] = {}
    for key in _COUNTED_FIELDS:
        value = params.get(key)
        if value is None:
            continue
        if key in ("system", "tools"):
            value = _without_cache_control(value)
        elif key == "messages":
            value = _messages_without_cache_control(value)
        out[key] = copy.deepcopy(value)
    return out


def count_request_digest(count_request: Mapping[str, Any]) -> str:
    """SHA-256 of the complete counting form, the model included."""
    payload = json.dumps(
        count_request,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _count_content(content: Any, counter: Callable[[str], int]) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return int(counter(content))
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text":
                total += int(counter(str(block.get("text") or "")))
            elif isinstance(block, str):
                total += int(counter(block))
            else:
                total += int(counter(json.dumps(block, sort_keys=True, default=str)))
        return total
    return int(counter(str(content)))


def local_request_tokens(
    count_request: Mapping[str, Any], *, counter: Callable[[str], int]
) -> int:
    """Local count of every counted part of a request (unpadded).

    System text, every message's content, each tool definition (as JSON),
    ``tool_choice``, and — when any tool is present —
    :data:`TOOL_USE_SYSTEM_PROMPT_ALLOWANCE`. ``counter`` is the local
    tokenizer (``tokenizer.count_tokens`` in production); it may raise, and
    the caller treats that as "no local estimate".
    """
    total = _count_content(count_request.get("system"), counter)
    for message in count_request.get("messages") or []:
        if isinstance(message, Mapping):
            total += _count_content(message.get("content"), counter)
    tools = count_request.get("tools") or []
    for tool in tools:
        total += int(counter(json.dumps(tool, sort_keys=True, ensure_ascii=False, default=str)))
    if count_request.get("tool_choice") is not None:
        total += int(counter(json.dumps(count_request["tool_choice"], sort_keys=True)))
    if tools:
        total += TOOL_USE_SYSTEM_PROMPT_ALLOWANCE
    return total


# ---------------------------------------------------------------------------
# Count cache (API estimates only)
# ---------------------------------------------------------------------------


class _CountCache:
    """Bounded LRU of API estimates keyed by :func:`count_request_digest`."""

    def __init__(self, max_entries: int = _COUNT_CACHE_MAX_ENTRIES) -> None:
        self._max_entries = int(max_entries)
        self._entries: "OrderedDict[str, int]" = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[int]:
        with self._lock:
            value = self._entries.get(key)
            if value is None:
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return value

    def put(self, key: str, value: int) -> None:
        with self._lock:
            self._entries[key] = int(value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._entries),
                "max_entries": self._max_entries,
            }


_COUNT_CACHE = _CountCache()


def clear_count_cache() -> None:
    """Forget every cached API estimate (tests; a new API key changes nothing)."""
    _COUNT_CACHE.clear()


def count_cache_stats() -> dict:
    return _COUNT_CACHE.stats()


def cached_api_estimate(count_request: Mapping[str, Any]) -> Optional[int]:
    """The cached API estimate for this exact counting form, if any."""
    return _COUNT_CACHE.get(count_request_digest(count_request))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _default_client_factory() -> Any:
    from ..review.reviewer import _get_client

    return _get_client()


def resolve_input_count(
    count_request: Mapping[str, Any],
    *,
    use_api: bool = True,
    client_factory: Optional[Callable[[], Any]] = None,
    call_gate: Any = None,
    local_counter: Optional[Callable[[str], int]] = None,
    include_local: bool = False,
) -> InputCount:
    """Size one counting form: API estimate if possible, else padded local.

    ``use_api=False`` never calls the endpoint but still uses an estimate a
    previous call cached for this exact form (the batch builder's path: it
    decides from the same basis the preflight used, without a network call).
    The endpoint is also skipped when ``api_config.token_count_preflight_enabled``
    says so. ``call_gate`` is held around the network call only.
    ``include_local`` takes the local count even when an API estimate exists
    (for a log line); otherwise it is taken only for the fallback.
    """
    model = str(count_request.get("model") or "")
    reason: Optional[str] = None
    digest = count_request_digest(count_request)
    api_tokens = _COUNT_CACHE.get(digest)
    if api_tokens is None:
        if not use_api:
            reason = "not counted by the API for this request shape"
        elif not _api_config.token_count_preflight_enabled():
            reason = "token-count preflight is disabled"
        else:
            factory = client_factory or _default_client_factory
            try:
                client = factory()
            except Exception as exc:  # noqa: BLE001 — reported, never raised
                client = None
                reason = f"no API client available ({type(exc).__name__}: {exc})"
            if client is not None:
                result = _tokenizer.count_input_tokens(
                    client=client, call_gate=call_gate, **count_request
                )
                if result.ok:
                    api_tokens = int(result.tokens)
                    _COUNT_CACHE.put(digest, api_tokens)
                else:
                    reason = result.error or "the count API returned no usable count"

    local_tokens: Optional[int] = None
    local_error: Optional[str] = None
    if api_tokens is None or include_local:
        counter = local_counter or _tokenizer.count_tokens
        try:
            local_tokens = local_request_tokens(count_request, counter=counter)
        except Exception as exc:  # noqa: BLE001 — EncoderLoadError and kin
            local_error = f"the local tokenizer failed ({type(exc).__name__}: {exc})"

    if api_tokens is not None:
        return InputCount(
            tokens=api_tokens,
            source=COUNT_SOURCE_API,
            local_tokens=local_tokens,
        )
    if local_tokens is not None:
        factor = _tokenizer.local_estimate_safety_factor(model)
        return InputCount(
            tokens=_tokenizer.safe_local_estimate(local_tokens, model=model),
            source=COUNT_SOURCE_LOCAL_PADDED,
            local_tokens=local_tokens,
            padding_factor=factor,
            unavailable_reason=reason,
        )
    return InputCount(
        tokens=None,
        source=COUNT_SOURCE_UNAVAILABLE,
        unavailable_reason="; ".join(filter(None, (reason, local_error))) or None,
    )


def budget_for_count(
    count: InputCount,
    *,
    model: str,
    output_reserve: int,
    phase_limit: Optional[int] = None,
) -> RequestBudget:
    """Turn a resolved input count into a fit decision for ``model``."""
    window, reserve, ceiling = input_ceiling_for(
        model, output_reserve=output_reserve, phase_limit=phase_limit
    )
    fits = count.tokens is not None and count.tokens <= ceiling
    return RequestBudget(
        model=model,
        count=count.tokens,
        count_source=count.source,
        context_window=window,
        output_reserve=int(output_reserve),
        safety_reserve=reserve,
        phase_limit=phase_limit,
        input_ceiling=ceiling,
        fits=fits,
        local_tokens=count.local_tokens,
        padding_factor=count.padding_factor,
        unavailable_reason=count.unavailable_reason,
    )


def request_budget(
    params: Mapping[str, Any],
    *,
    phase_limit: Optional[int] = None,
    use_api: bool = True,
    client_factory: Optional[Callable[[], Any]] = None,
    call_gate: Any = None,
    local_counter: Optional[Callable[[str], int]] = None,
    include_local: bool = False,
) -> RequestBudget:
    """The budget of one fully built request (its ``max_tokens`` included)."""
    model = str(params.get("model") or "")
    count = resolve_input_count(
        count_request_from_params(params),
        use_api=use_api,
        client_factory=client_factory,
        call_gate=call_gate,
        local_counter=local_counter,
        include_local=include_local,
    )
    return budget_for_count(
        count,
        model=model,
        output_reserve=int(params.get("max_tokens") or 0),
        phase_limit=phase_limit,
    )


def oversize_reason(budget: RequestBudget, *, what: str) -> str:
    """One sentence saying why ``what`` was not sent (never truncated)."""
    if budget.count is None:
        return (
            f"The {what} was not sent because its size could not be determined "
            f"({budget.unavailable_reason or 'no count available'}). Nothing was "
            "truncated."
        )
    return (
        f"The {what} needs {budget.size_text()}, over the input ceiling of "
        f"{budget.input_ceiling:,} for {budget.model} (its "
        f"{budget.context_window:,}-token context window, less the request's "
        f"{budget.output_reserve:,}-token output cap and a "
        f"{budget.safety_reserve:,}-token safety reserve"
        + (
            f", capped at the phase limit of {budget.phase_limit:,}"
            if budget.phase_limit is not None
            and budget.input_ceiling == budget.phase_limit
            else ""
        )
        + "). It was not sent, and nothing was truncated."
    )
