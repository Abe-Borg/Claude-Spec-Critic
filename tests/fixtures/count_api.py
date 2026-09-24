"""Scripted ``count_tokens`` answers for request-budget tests (plan WP-08).

The live endpoint is disabled in hermetic tests
(``tests/conftest.py::_no_live_token_counting``), so a request budget falls
back to its padded local estimate unless the test's fake client can count. A
test that exercises the API-estimate path gives its fake a
``messages.count_tokens`` whose answer the test chooses — usually a function
of the counting form the code under test actually sent, so a chunk's
"estimate" grows with the specs in it. The helpers here read that form.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable


def user_text(request: dict) -> str:
    """The text of the first message's content (the corpus the pass sends)."""
    content = (request.get("messages") or [{}])[0].get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text") or "") for block in content if isinstance(block, dict)
        )
    return ""


def spec_blocks(request: dict) -> int:
    """How many ``<spec`` blocks the request's corpus carries."""
    return user_text(request).count("<spec ")


def user_words(request: dict) -> int:
    """Whitespace-separated words in the request's first message."""
    return len(user_text(request).split())


def count_response(tokens: Any) -> SimpleNamespace:
    """A ``MessageTokensCount``-shaped response carrying ``tokens`` verbatim."""
    return SimpleNamespace(input_tokens=tokens)


class CountingMessages:
    """``client.messages`` double answering ``count_tokens`` with ``count_fn``.

    Records every counting form it receives in ``count_calls``. ``gate``
    (optional) is any object with a boolean ``held``; its state at each call
    is recorded in ``held_at_count`` so a test can prove the permit covered
    the call.
    """

    def __init__(self, count_fn: Callable[[dict], Any], *, gate=None) -> None:
        self.count_fn = count_fn
        self.gate = gate
        self.count_calls: list[dict] = []
        self.held_at_count: list[bool] = []

    def count_tokens(self, **kwargs):
        self.count_calls.append(kwargs)
        if self.gate is not None:
            self.held_at_count.append(bool(getattr(self.gate, "held", False)))
        return count_response(self.count_fn(kwargs))


class CountingClient:
    """A client that can only count (no streaming)."""

    def __init__(self, count_fn: Callable[[dict], Any], *, gate=None) -> None:
        self.messages = CountingMessages(count_fn, gate=gate)

    @property
    def count_calls(self) -> list[dict]:
        return self.messages.count_calls
