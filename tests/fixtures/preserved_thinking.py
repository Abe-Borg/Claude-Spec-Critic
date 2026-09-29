"""A model of the API's preserved-thinking check, for tests.

Anthropic's "Preserved thinking" contract for Claude Opus 5.5, Sonnet 5.5, and
Fable 5.1 (platform.claude.com/docs/en/build-with-claude/preserved-thinking,
checked 2026-09-29), enforced by default for accounts created on or after
2026-08-31 00:00 UTC (HTTP 400 on the Messages API; the Message Batches API
drops the failing blocks instead):

* a thinking block stays valid only while everything sent before it is
  unchanged: the ``system`` prompt, the ``tools``, and every message before
  it. Earlier thinking blocks are not part of that prefix;
* each thinking block records the thinking block before it, across turns, so
  the blocks kept must be an unbroken run of the original sequence: removing
  blocks from the start, from the end, or all of them is valid, a gap is not;
* a block once removed stays out; sending it again is invalid.

:func:`preserved_thinking_violations` checks a sequence of requests against
those rules. Each request is paired with the content its response returned,
because a block's prefix is what the model saw when it produced the block —
the request, then the response's own earlier blocks — and a check that read
that prefix off a later request could not see an edit made before the block's
first replay (an elided PDF earlier in the same response, say).

What it does not model: the check compares normalized content (dicts with
``None`` fields dropped), so it cannot see a change in how a block is
serialized; consecutive messages of one role are treated as one run of blocks,
as the API combines them; and blocks are identified by their ``signature``
(``data`` for a redacted block), so a test must give each block its own.
"""
from __future__ import annotations

import dataclasses
import json
from typing import Any, Iterable

THINKING_TYPES = ("thinking", "redacted_thinking")


def _plain(node: Any) -> Any:
    """``node`` as JSON-able data, the way a request body would carry it."""
    dump = getattr(node, "model_dump", None)
    if callable(dump):
        node = dump(mode="json", exclude_none=True)
    elif dataclasses.is_dataclass(node) and not isinstance(node, type):
        node = dataclasses.asdict(node)
    if isinstance(node, dict):
        return {key: _plain(value) for key, value in node.items() if value is not None}
    if isinstance(node, (list, tuple)):
        return [_plain(item) for item in node]
    return node


def _canonical(node: Any) -> str:
    return json.dumps(_plain(node), sort_keys=True)


def _blocks(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [_plain(block) for block in content or []]


def _is_thinking(block: dict) -> bool:
    return block.get("type") in THINKING_TYPES


def _key(block: dict) -> str:
    return block.get("signature") or block.get("data") or ""


def preserved_thinking_violations(
    exchanges: Iterable[tuple[dict, Any]],
) -> list[str]:
    """What an account enforced on preserved thinking would reject.

    ``exchanges`` is the conversation's requests in order, each paired with
    the content its response returned: ``(body, response_content)``, where
    ``body`` has ``system``, ``tools``, and ``messages`` as sent (a missing
    ``system`` or ``tools`` counts as empty). The last pair's response may be
    ``None``. Returns one line per violation; an empty list means every
    request would pass.
    """
    problems: list[str] = []
    # key -> (system, tools, the non-thinking content before the block)
    origin: dict[str, tuple[str, str, tuple]] = {}
    # key -> the thinking block before it when it was produced
    before: dict[str, str | None] = {}
    produced: set[str] = set()
    removed: set[str] = set()
    for n, (body, response) in enumerate(exchanges):
        system = _canonical(body.get("system"))
        tools = _canonical(body.get("tools"))
        prefix: list[tuple[str, str]] = []
        present: list[str] = []
        for message in body["messages"]:
            role = message["role"] if isinstance(message, dict) else message.role
            content = message["content"] if isinstance(message, dict) else message.content
            for block in _blocks(content):
                if not _is_thinking(block):
                    prefix.append((role, _canonical(block)))
                    continue
                key = _key(block)
                where = f"request {n}, thinking block {key!r}"
                if key not in produced:
                    problems.append(f"{where}: not produced by an earlier response")
                else:
                    if key in removed:
                        problems.append(f"{where}: sent again after it was removed")
                    if (system, tools, tuple(prefix)) != origin[key]:
                        problems.append(
                            f"{where}: the conversation before it changed since it was produced"
                        )
                    if present and present[-1] != before[key]:
                        problems.append(f"{where}: a gap in the thinking blocks before it")
                if key in present:
                    problems.append(f"{where}: sent twice in one request")
                present.append(key)
        removed |= produced - set(present)
        if response is None:
            continue
        # The response continues the request: its blocks' prefix is every
        # non-thinking block sent, then its own earlier blocks.
        last = present[-1] if present else None
        response_prefix = list(prefix)
        for block in _blocks(response):
            if not _is_thinking(block):
                response_prefix.append(("assistant", _canonical(block)))
                continue
            key = _key(block)
            origin[key] = (system, tools, tuple(response_prefix))
            before[key] = last
            produced.add(key)
            last = key
    return problems
