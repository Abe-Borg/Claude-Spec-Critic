"""No-op tracing shim.

The original host app shipped a forensic tracer; the standalone analyzer does
not. ``batch_digest`` calls :func:`capture_note` defensively, so a no-op keeps
that call site unchanged. Replace this with a real recorder if tracing is ever
wanted.
"""
from __future__ import annotations

from typing import Any


def capture_note(*args: Any, **kwargs: Any) -> None:
    """Defensive no-op — the batch-digest path calls this; nothing records it."""
    return None
