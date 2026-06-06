"""Pure (tkinter-free) helpers for assembling Project Context attachments.

Kept separate from :mod:`context_controller` (which imports
customtkinter / tkinter, and therefore can't be imported in a headless test
environment) so the merge + token-cap + attachment-wrapping logic can be unit
tested without the GUI stack — and *without* PyMuPDF, because a drawing digest
is duck-typed here (only its combined text and sheet / file counts are read,
never the drawing engine itself).

Both context-attachment flows in :mod:`context_controller` — the synchronous
``.docx`` / ``.pdf`` / ``.md`` / ``.txt`` file attach and the threaded
"Attach Drawings…" digest attach — funnel through these helpers so the
delimiter shape and the hard ``PROJECT_CONTEXT_MAX_TOKENS`` cap stay identical.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..core.tokenizer import PROJECT_CONTEXT_MAX_TOKENS, count_tokens


def wrap_attachment(label: str, text: str) -> str:
    """Wrap ``text`` in the BEGIN/END ATTACHMENT delimiters used for context.

    The delimiters give the model a clear boundary around reference material
    spliced into the free-text Project Context, mirroring the long-standing
    file-attachment shape so a downstream prompt-cache prefix stays stable.
    """
    return f"--- BEGIN ATTACHMENT: {label} ---\n{text}\n--- END ATTACHMENT: {label} ---"


class DrawingDigestLike(Protocol):
    """The duck-typed surface of a ``DrawingContext`` that this module reads.

    Declared so tests can inject a lightweight fake (no PyMuPDF, no rendering)
    and so the coupling to the drawing engine is documented and minimal.
    """

    combined_text: str
    sheet_count: int
    file_count: int

    @property
    def ok_sheet_count(self) -> int: ...

    errors: list[str]


def build_drawing_attachment_block(ctx: DrawingDigestLike) -> str:
    """Wrap a drawing digest's combined text as a labeled context attachment.

    Returns ``""`` when nothing usable was digested — either no body text, or a
    **fully-failed set** (``ok_sheet_count == 0``). The engine's
    ``combined_text`` is non-empty even when every sheet fails (it still emits a
    header plus a per-sheet failure blockquote), so the gate is ``ok_sheet_count``
    — *not* body text alone — to stop an attachment of pure failure messages
    from being spliced into context (the per-sheet errors surface via the GUI
    warning instead). The label records how many sheets actually digested
    (``ok``/``total``) so the reviewer can see at a glance whether the set was
    fully read.
    """
    if ctx.ok_sheet_count <= 0:
        return ""
    body = (getattr(ctx, "combined_text", "") or "").strip()
    if not body:
        return ""
    label = (
        f"DRAWING DIGEST — {ctx.ok_sheet_count}/{ctx.sheet_count} sheet(s) "
        f"from {ctx.file_count} file(s)"
    )
    return wrap_attachment(label, body)


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


@dataclass(frozen=True)
class DrawingAttachmentPlan:
    """The decision a drawing-digest attach resolves to, computed off-thread-safe.

    Pure data the thin GUI completion handler renders: which messages to show
    (per-sheet ``error_lines``), whether there is anything to attach
    (:attr:`has_digest`), whether it fits the cap (:attr:`within_cap`), and the
    exact merged context + token count to commit when :attr:`attachable`.
    """

    block: str
    merged_context: str
    tokens: int
    within_cap: bool
    error_lines: list[str] = field(default_factory=list)

    @property
    def has_digest(self) -> bool:
        return bool(self.block)

    @property
    def attachable(self) -> bool:
        return self.has_digest and self.within_cap


def plan_drawing_attachment(
    existing_context: str, ctx: DrawingDigestLike
) -> DrawingAttachmentPlan:
    """Resolve how a drawing digest should fold into the existing context.

    Builds the labeled attachment block, merges it after the existing context,
    and checks the merged result against the token cap — returning all the facts
    the GUI needs without touching any widget, so the decision is unit-testable
    with a fake ``DrawingContext`` and no GUI/PyMuPDF/network dependency.
    """
    error_lines = list(getattr(ctx, "errors", None) or [])
    block = build_drawing_attachment_block(ctx)
    if not block:
        return DrawingAttachmentPlan(
            block="",
            merged_context=(existing_context or "").strip(),
            tokens=0,
            within_cap=True,
            error_lines=error_lines,
        )
    merged = merge_into_context(existing_context, block)
    tokens, fits = context_within_token_cap(merged)
    return DrawingAttachmentPlan(
        block=block,
        merged_context=merged,
        tokens=tokens,
        within_cap=fits,
        error_lines=error_lines,
    )
