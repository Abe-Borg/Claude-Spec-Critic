"""Pure (tkinter-free) helpers for assembling Project Context attachments.

Kept separate from :mod:`context_controller` (which imports
customtkinter / tkinter, and therefore can't be imported in a headless test
environment) so the merge + token-cap + attachment-wrapping logic can be unit
tested without the GUI stack.

These back the synchronous ``.docx`` / ``.pdf`` / ``.md`` / ``.txt`` file-attach
flow in :mod:`context_controller`.
"""
from __future__ import annotations

from ..core.tokenizer import PROJECT_CONTEXT_MAX_TOKENS, count_tokens


def wrap_attachment(label: str, text: str) -> str:
    """Wrap ``text`` in the BEGIN/END ATTACHMENT delimiters used for context.

    The delimiters give the model a clear boundary around reference material
    spliced into the free-text Project Context, mirroring the long-standing
    file-attachment shape so a downstream prompt-cache prefix stays stable.
    """
    return f"--- BEGIN ATTACHMENT: {label} ---\n{text}\n--- END ATTACHMENT: {label} ---"


def merge_into_context(existing: str, addition: str) -> str:
    """Append ``addition`` to ``existing`` Project Context, blank-line separated.

    Both sides are stripped; a blank ``addition`` returns the existing context
    unchanged, and a blank ``existing`` returns the addition alone (no leading
    separator). This is the single merge shape used by every attach path.
    """
    existing = (existing or "").strip()
    addition = (addition or "").strip()
    if not addition:
        return existing
    return f"{existing}\n\n{addition}" if existing else addition


def context_within_token_cap(text: str) -> tuple[int, bool]:
    """Return ``(token_count, fits)`` for ``text`` vs ``PROJECT_CONTEXT_MAX_TOKENS``.

    ``fits`` is ``True`` when the count is at or below the cap. Callers refuse
    (never truncate) an over-cap merge; surfacing the exact count lets the error
    message tell the operator how far over they are.
    """
    tokens = count_tokens(text)
    return tokens, tokens <= PROJECT_CONTEXT_MAX_TOKENS
