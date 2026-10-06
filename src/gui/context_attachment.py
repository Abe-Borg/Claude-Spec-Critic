"""Pure (tkinter-free) helpers for assembling Project Context attachments.

Kept separate from :mod:`context_controller` (which imports
customtkinter / tkinter, and therefore can't be imported in a headless test
environment) so the merge + token-cap + attachment-wrapping logic can be unit
tested without the GUI stack.

These back the ``.docx`` / ``.pdf`` / ``.md`` / ``.txt`` file-attach flow and
the drawing-analysis attach flow in :mod:`context_controller`, and the
FILES-panel readout of the drawing analyses currently in Project Context.
"""
from __future__ import annotations

from ..core.tokenizer import PROJECT_CONTEXT_MAX_TOKENS, count_tokens


_ATTACHMENT_MARKER_PREFIXES = ("--- BEGIN ATTACHMENT: ", "--- END ATTACHMENT: ")
ATTACHMENT_MARKER_ESCAPE = "\\"


def escape_attachment_markers(text: str) -> str:
    """Neutralize any line of ``text`` that would read as an attachment delimiter.

    A line that starts exactly like a BEGIN/END ATTACHMENT marker gets a
    leading backslash, so content that happens to contain one (an analyzer
    output that quotes a previous Project Context, say) cannot close a block
    early. The readers that key off the markers (the drawing-digest parser
    and the drawing-impact gate) require the marker at the start of a line,
    so a one-character prefix is enough and the text stays readable. A line
    already escaped is left alone, so the transform is idempotent.
    """
    if not text:
        return text
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.startswith(_ATTACHMENT_MARKER_PREFIXES):
            lines[index] = ATTACHMENT_MARKER_ESCAPE + line
    return "\n".join(lines)


def wrap_attachment(label: str, text: str) -> str:
    """Wrap ``text`` in the BEGIN/END ATTACHMENT delimiters used for context.

    The delimiters give the model a clear boundary around reference material
    spliced into the free-text Project Context, mirroring the long-standing
    file-attachment shape so a downstream prompt-cache prefix stays stable.
    Delimiter-shaped lines inside ``text`` are escaped first
    (:func:`escape_attachment_markers`), so the block always ends at its own
    END marker; ordinary content is byte-identical to before.
    """
    body = escape_attachment_markers(text)
    return f"--- BEGIN ATTACHMENT: {label} ---\n{body}\n--- END ATTACHMENT: {label} ---"


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
    (never truncate) an over-cap merge; surfacing the count lets the error
    message tell the operator how far over they are.
    """
    tokens = count_tokens(text)
    return tokens, tokens <= PROJECT_CONTEXT_MAX_TOKENS


def context_has_drawing_digest(context: str | None) -> bool:
    """True when ``context`` still contains at least one drawing-digest block.

    Matches the exact ``BEGIN ATTACHMENT`` marker the drawing-analysis attach
    flow writes (see :mod:`src.input.drawing_analysis`), so a context file
    merely *named* like the digest (it carries a file extension in its label)
    never false-positives.
    """
    # Lazy import: drawing_analysis imports wrap_attachment from this module,
    # so a top-level import here would be circular.
    from ..input.drawing_analysis import drawing_analysis_blocks

    return bool(drawing_analysis_blocks(context))


def drawing_analysis_readout(
    context: str | None, *, token_memo: dict[str, int] | None = None
) -> list[dict]:
    """FILES-panel rows for the drawing analyses in ``context``.

    Returns ``[{"name": <file name>, "tokens": <count>}]``, one row per
    ``Construction Drawing Digest`` block, in order. It is a pure function of
    the textbox contents, recomputed after every settled edit: the row for a
    block the operator deletes by hand disappears, and the count of one they
    trim shrinks. The count is the local tokenizer's (the same count the
    Project Context label shows), of the block body without its source line —
    so it matches the figure shown when the file was attached.

    ``token_memo`` (``{block text: tokens}``) lets the caller skip re-counting
    a block whose text did not change between edits; it is pruned to the
    blocks still present.
    """
    from ..input.drawing_analysis import drawing_analysis_blocks

    rows: list[dict] = []
    fresh: dict[str, int] = {}
    for block in drawing_analysis_blocks(context):
        tokens = None if token_memo is None else token_memo.get(block.text)
        if tokens is None:
            tokens = count_tokens(block.text)
        fresh[block.text] = tokens
        rows.append({"name": block.display_name, "tokens": tokens})
    if token_memo is not None:
        token_memo.clear()
        token_memo.update(fresh)
    return rows
