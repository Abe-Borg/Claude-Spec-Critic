"""API-key scrubbing for trace artifacts.

Imports the regex patterns from ``src.orchestration.diagnostics`` so the
two surfaces (diagnostics + tracing) share one source of truth. A future
addition of a new credential pattern only needs to be made in diagnostics.

Spec content is intentionally NOT scrubbed — per user direction, the
trace is allowed to capture the full extracted spec text. Only
credential-shaped values are redacted.
"""
from __future__ import annotations

from typing import Any

from ..orchestration.diagnostics import (
    _MAX_DEPTH_MARKER,
    _REDACTED,
    _SECRET_KEY_PATTERN,
    _SECRET_VALUE_PATTERNS,
)


def scrub_value(value: Any) -> Any:
    """Replace credential-shaped strings with ``"<redacted>"``.

    Non-string values pass through unchanged. The patterns intentionally
    look for the full key prefix (``sk-ant-``, ``Bearer ``, ``AKIA``)
    rather than just any hex run, so false positives are rare.
    """
    if not isinstance(value, str):
        return value
    for pattern in _SECRET_VALUE_PATTERNS:
        if pattern.search(value):
            return _REDACTED
    return value


def scrub_data(data: Any, *, _depth: int = 0) -> Any:
    """Recursively scrub a JSON-ready structure.

    Keys that look secret-shaped (``api_key``, ``password``, etc.)
    redact their value entirely; other string values get matched against
    the credential prefix patterns. Recursion is bounded at six levels so a
    cyclic dict cannot loop forever.

    Past the bound a **container** collapses to ``_MAX_DEPTH_MARKER``. It
    previously returned ``repr(data)``, which rendered every value in the
    subtree verbatim — so the deeper a credential sat, the *less* protected it
    was, exactly inverting the intent. A **scalar** past the bound is still
    scrubbed: it cannot recurse, and dropping it would lose numeric telemetry
    and redact-able strings for nothing.
    """
    if _depth > 6:
        if isinstance(data, (dict, list, tuple, set)):
            return _MAX_DEPTH_MARKER
        return scrub_value(data)
    if isinstance(data, dict):
        out: dict = {}
        for key, value in data.items():
            if isinstance(key, str) and _SECRET_KEY_PATTERN.search(key):
                out[key] = _REDACTED
                continue
            out[key] = scrub_data(value, _depth=_depth + 1)
        return out
    if isinstance(data, list):
        return [scrub_data(v, _depth=_depth + 1) for v in data]
    if isinstance(data, tuple):
        return tuple(scrub_data(v, _depth=_depth + 1) for v in data)
    return scrub_value(data)
