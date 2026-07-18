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


def context_has_drawing_digest(context: str | None) -> bool:
    """True when ``context`` still contains a drawing-digest attachment block.

    Lets the GUI keep the FILES-panel drawing readout in sync with the
    textbox: if the operator deletes the merged digest by hand, the readout
    clears. Matches the exact ``BEGIN ATTACHMENT`` marker
    :func:`wrap_attachment` writes for the digest, so a context file merely
    *named* like the digest (it carries a file extension in its label) never
    false-positives.
    """
    if not context:
        return False
    # Lazy import: drawing_digest imports wrap_attachment from this module,
    # so a top-level import here would be circular. The label is resolved at
    # call time (drawing_digest is fully loaded by then) to keep one source
    # of truth for the marker string.
    from ..input.drawing_digest import DIGEST_ATTACHMENT_LABEL

    return f"--- BEGIN ATTACHMENT: {DIGEST_ATTACHMENT_LABEL} ---" in context


def digested_drawing_filenames(chunk_statuses) -> list[str]:
    """Base filenames whose sheets landed in a non-failed digest chunk.

    ``chunk_statuses`` is any iterable of objects exposing ``.status`` and
    ``.file_labels`` (a ``DrawingDigestResult``'s ``chunk_statuses``). A
    *failed* chunk's sheets are not in the digest text — the partial-failure
    warning names them — so they're excluded from the FILES-panel readout.
    Labels are ``"plans.pdf"`` or ``"plans.pdf (pages 301-600)"``; the page
    suffix is stripped to recover the filename. Order-preserving and de-duped,
    so a file split across several chunks appears once.
    """
    names: list[str] = []
    seen: set[str] = set()
    for status in chunk_statuses:
        if getattr(status, "status", "") == "failed":
            continue
        for label in getattr(status, "file_labels", ()) or ():
            name = label.split(" (pages ")[0].strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def drawing_filenames_with_failed_chunks(chunk_statuses) -> set[str]:
    """Base filenames that had at least one *failed* digest chunk.

    A large PDF can be split into page-range chunks; when only some of its
    chunks fail, the file still appears in :func:`digested_drawing_filenames`
    (its surviving ranges are in the digest), but its *full* page count would
    overstate what actually landed in Project Context. Callers use this to
    drop the page count for such partial files — the filename still shows,
    and the separate partial-failure warning names the missing ranges. Labels
    are stripped of their ``" (pages ...)"`` suffix, mirroring
    :func:`digested_drawing_filenames`.
    """
    failed: set[str] = set()
    for status in chunk_statuses:
        if getattr(status, "status", "") != "failed":
            continue
        for label in getattr(status, "file_labels", ()) or ():
            name = label.split(" (pages ")[0].strip()
            if name:
                failed.add(name)
    return failed
