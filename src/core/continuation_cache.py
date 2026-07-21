"""Message-level prompt-cache breakpoint for pause_turn continuation resumes.

The pause_turn contract re-sends the whole growing conversation on every
continuation. The request-level breakpoints (system prompt + trailing tool,
``api_config.system_prompt_with_cache`` / ``tools_with_cache``) only cover
the static prefix — the accumulated assistant turns behind them re-bill as
uncached input on every resume. Observed live: one research dimension
accumulated 204k uncached input tokens across its pause_turn continuations.

``mark_continuation_cache_breakpoint`` closes that gap: it places exactly one
``cache_control`` breakpoint on the last cache-eligible content block of the
last assistant message, so the next resume's conversation prefix (everything
up to and including that block) reads from cache. A strip pass first removes
any breakpoint a previous continuation placed, keeping the total at
system + last-tool + one message-level marker = 3 breakpoints, under the
API's limit of 4.

Block eligibility is conservative:

* ``thinking`` / ``redacted_thinking`` blocks never take ``cache_control``
  (the API rejects it), so the walk skips past a trailing thinking block.
* Server-tool blocks (``server_tool_use``, ``web_search_tool_result``,
  ``web_fetch_tool_result``) are skipped too — their cache_control
  eligibility is undocumented, and a plain ``text`` / ``tool_use`` block is
  always nearby in a paused turn.

Copy-on-write throughout: SDK response objects are never mutated (traces and
the evidence collectors keep reading pristine data); only the messages that
change are rebuilt as plain dicts. When there is nothing to mark, the input
list is returned unchanged.

Call order at a resume site: append the assistant turn →
``sanitize_messages_for_resend`` → ``mark_continuation_cache_breakpoint``.
Sanitizing may rebuild the just-appended message (PDF elision), so marking
last guarantees the breakpoint survives.
"""
from __future__ import annotations

from typing import Any

from .api_config import cache_control_block
from .resend_sanitizer import to_plain_block

# Block types that accept ``cache_control`` per the prompt-caching docs.
_ELIGIBLE_BLOCK_TYPES = frozenset(
    {"text", "tool_use", "tool_result", "document", "image"}
)


def _get(node: Any, key: str) -> Any:
    if isinstance(node, dict):
        return node.get(key)
    return getattr(node, key, None)


def _has_message_breakpoint(message: Any) -> bool:
    content = _get(message, "content")
    if not isinstance(content, list):
        return False
    return any(_get(block, "cache_control") for block in content)


def _last_eligible_index(content: list) -> int | None:
    for idx in range(len(content) - 1, -1, -1):
        if _get(content[idx], "type") in _ELIGIBLE_BLOCK_TYPES:
            return idx
    return None


def mark_continuation_cache_breakpoint(messages: list[dict]) -> list[dict]:
    """Place exactly one message-level cache breakpoint for the next resume.

    Marks the last eligible block of the last assistant message and strips
    any message-level breakpoint a previous continuation placed. Returns the
    input list unchanged (same object) when no assistant message carries an
    eligible block; otherwise returns a new list in which only the affected
    messages are rebuilt — inputs and SDK objects are never mutated.
    """
    target_msg_idx: int | None = None
    target_block_idx: int | None = None
    for msg_idx in range(len(messages) - 1, -1, -1):
        if _get(messages[msg_idx], "role") != "assistant":
            continue
        content = _get(messages[msg_idx], "content")
        if isinstance(content, list):
            block_idx = _last_eligible_index(content)
            if block_idx is not None:
                target_msg_idx = msg_idx
                target_block_idx = block_idx
        break
    if target_msg_idx is None or target_block_idx is None:
        return messages

    marked = list(messages)
    for msg_idx, message in enumerate(messages):
        needs_strip = _has_message_breakpoint(message)
        if msg_idx != target_msg_idx and not needs_strip:
            continue
        content = _get(message, "content")
        new_content = [to_plain_block(block) for block in content]
        if needs_strip:
            for block in new_content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
        if msg_idx == target_msg_idx:
            target = new_content[target_block_idx]
            if not isinstance(target, dict):
                # Unconvertible SDK block — leave the conversation unmarked
                # rather than risk mutating a response object.
                return messages
            target["cache_control"] = cache_control_block()
        marked[msg_idx] = {
            "role": _get(message, "role") or "assistant",
            "content": new_content,
        }
    return marked
