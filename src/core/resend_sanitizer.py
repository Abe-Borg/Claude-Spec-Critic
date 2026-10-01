"""Sanitize assistant content before a ``pause_turn`` continuation resume.

The ``web_fetch`` server tool returns fetched PDFs as ``document`` blocks
with a base64 ``application/pdf`` source inside the ``web_fetch_tool_result``
block. The pause_turn contract re-sends the assistant content verbatim, which
turns those fetched documents into *inbound* PDFs on the continuation
request — and the Messages API enforces its per-request PDF page limit on
inbound content regardless of the fact that it produced the bytes itself.
Fetching a large code document (e.g. a full building code, easily >600
pages) and then pausing therefore kills the continuation with HTTP 400:

    messages.N.content.M.pdf.source.base64.data: A maximum of 600 PDF
    pages may be provided.

``sanitize_messages_for_resend`` is the shared guard applied at every
continuation resume site (research realtime loop, verifier realtime loop,
and the batch continuation builder in ``verification_routing``). It counts
the pages of every fetched base64 PDF across the conversation's assistant
messages and, only when the total exceeds the API limit, replaces the
largest offenders' PDF payloads with a short plain-text elision note until
the total fits. A conversation with no fetched PDFs — or whose PDFs fit
inside the limit — is returned as the *same list object*, byte-identical
(the dominant path costs one shallow scan).

Eliding edits replayed history, which preserved thinking forbids. On Claude
Opus 5.5 and Sonnet 5.5 (and Fable 5.1) a thinking block stays valid only
while the ``system`` prompt, the ``tools``, and every message before it are
unchanged since it was produced; shortening an earlier tool result
invalidates every later thinking block, and accounts created on or after
2026-08-31 00:00 UTC get HTTP 400 for replaying one (the Message Batches API
drops the failing blocks instead). Removing thinking blocks from the start of
the history, from the end, or all of them is valid; a gap is not, and a
removed block must never be sent again (Anthropic, "Preserved thinking",
checked 2026-09-29). So an eliding pass also removes every ``thinking`` /
``redacted_thinking`` block after the earliest PDF it elides, across all
later assistant content, and drops an assistant message left empty:
the blocks kept are the unbroken run before the cut, whose prefixes did not
change. It holds for both kinds of caller. The real-time loops carry the
sanitized list forward, so a removed block is gone for good, and blocks
produced after the cut (with the PDF already elided) are kept until a later
pass elides again. The batch continuation builder re-sanitizes the original
blocks on every wave, and the set of PDFs a pass elides only grows as the
conversation does (largest first, ties in document order), so the cut only
moves earlier and a block once removed stays removed. The alternative, the
request field ``thinking.block_binding.prefix_mismatch_behavior:
"drop_block"``, drops each invalid block and every thinking block after it
server-side: the same blocks at the eliding resume, but on every later
real-time resume the valid thinking produced after it too, since the invalid
blocks are still sent. It also needs the
``thinking-binding-controls-2026-08-01`` beta header on every resume, and a
retired beta value is itself a 400 (see the web-fetch beta precedent in
CLAUDE.md).

Dependency-light on purpose: stdlib + a lazy ``pypdf`` import (already a
runtime dependency, used by the context-attachment extractor). Never
raises — an unparseable PDF is treated as un-countable and elided first,
which errs toward a request the API will accept.
"""
from __future__ import annotations

import base64
import copy
import dataclasses
import io
from typing import Any

# Mirror of the Messages API's per-request PDF page ceiling (the limit the
# 400 above names). Total across every PDF in the request, not per document.
MAX_RESEND_PDF_PAGES = 600

_ELISION_NOTE = (
    "[Fetched PDF content elided before continuation resume: {detail} "
    "The API accepts at most {limit} PDF pages per request, so this "
    "document's pages could not be re-sent. Its findings from the earlier "
    "turn remain above; re-fetch the source URL if more content is needed.]"
)

# Content blocks a preserved-thinking model binds to the conversation before
# them. Both kinds are removed after the earliest elided PDF.
_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})


def _get(node: Any, key: str) -> Any:
    """Read ``key`` from a dict or an attribute from an SDK/dataclass object."""
    if isinstance(node, dict):
        return node.get(key)
    return getattr(node, key, None)


def _find_pdf_sources(content: Any) -> list[tuple[int, Any]]:
    """Return ``(block index, source)`` for each base64 PDF in fetched documents.

    Targeted traversal (mirrors ``verifier._collect_fetch_evidence_detailed``
    rather than a blind recursive walk): assistant content can only carry
    PDFs inside ``web_fetch_tool_result`` blocks, whose single fetched
    document sits at ``block.content.content`` (current SDK) or
    ``block.content.document`` (older echo shape). Document order; the index
    is the block's position in ``content``.
    """
    sources: list[tuple[int, Any]] = []
    for block_idx, block in enumerate(content or []):
        if _get(block, "type") != "web_fetch_tool_result":
            continue
        result = _get(block, "content")
        if result is None:
            continue
        document = _get(result, "content") or _get(result, "document")
        if document is None:
            continue
        source = _get(document, "source")
        if source is None:
            continue
        if (
            _get(source, "type") == "base64"
            and _get(source, "media_type") == "application/pdf"
            and _get(source, "data")
        ):
            sources.append((block_idx, source))
    return sources


def _pdf_page_count(b64_data: Any) -> int | None:
    """Count a base64 PDF's pages; ``None`` when it cannot be determined."""
    try:
        from pypdf import PdfReader

        raw = base64.b64decode(b64_data)
        return len(PdfReader(io.BytesIO(raw)).pages)
    except Exception:  # noqa: BLE001 — un-countable is a valid outcome
        return None


def _to_plain_block(block: Any) -> Any:
    """Best-effort conversion of a content block to a mutable plain dict.

    Dicts are deep-copied (the caller mutates the copy, never the
    original response object — traces and evidence collectors keep reading
    pristine data). SDK pydantic models dump to JSON-mode dicts; dataclass
    fixtures convert via ``asdict``. An unconvertible block is returned
    as-is and its PDFs simply stay un-elided (defensive: no crash).
    """
    if isinstance(block, dict):
        return copy.deepcopy(block)
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json", exclude_none=True)
        except TypeError:
            try:
                return dump()
            except Exception:  # noqa: BLE001
                return block
        except Exception:  # noqa: BLE001
            return block
    if dataclasses.is_dataclass(block) and not isinstance(block, type):
        try:
            return dataclasses.asdict(block)
        except Exception:  # noqa: BLE001
            return block
    return block


def _elide_source(source: dict, *, pages: int | None) -> None:
    """Swap a base64 PDF source for a plain-text elision note, in place."""
    detail = (
        f"this document is {pages} pages."
        if pages is not None
        else "this document's page count could not be determined."
    )
    note = _ELISION_NOTE.format(detail=detail, limit=MAX_RESEND_PDF_PAGES)
    source.clear()
    source.update({"type": "text", "media_type": "text/plain", "data": note})


def sanitize_messages_for_resend(messages: list[dict]) -> list[dict]:
    """Ensure a continuation resume request fits the API's PDF page limit.

    Scans the **assistant** messages (fetched documents can only live
    there; the user prompts in this codebase are plain text) for base64
    PDF sources inside ``web_fetch_tool_result`` blocks. When the total
    page count exceeds :data:`MAX_RESEND_PDF_PAGES`, the offending PDF
    payloads are replaced — largest first, un-countable ones before any
    countable one — with a plain-text note until the remainder fits. The
    same pass then removes every thinking block after the earliest PDF it
    elided (see the module docstring: preserved thinking).

    Returns ``messages`` unchanged (same object) when nothing needs
    eliding, so the common no-PDF path stays byte-identical. When eliding,
    returns a new list in which only the affected messages are rebuilt: an
    elided fetch block is deep-copied (or dumped to a plain dict) and
    edited, every other block is kept as the same object it was, and an
    assistant message left with no blocks is dropped. The input and the
    underlying response objects are never mutated.
    """
    # Pass 1: locate + count every fetched PDF, reading originals in place.
    found: list[dict[str, Any]] = []  # {msg_idx, block_idx, pages}
    for msg_idx, message in enumerate(messages):
        if _get(message, "role") != "assistant":
            continue
        for block_idx, source in _find_pdf_sources(_get(message, "content")):
            found.append(
                {
                    "msg_idx": msg_idx,
                    "block_idx": block_idx,
                    "pages": _pdf_page_count(_get(source, "data")),
                }
            )
    if not found:
        return messages

    # Pass 2: decide which PDFs to elide. Un-countable PDFs go first (they
    # cannot be trusted against the ceiling); then largest-first among the
    # counted until the counted total fits under the limit. The sort is
    # stable, so equal sizes go in document order: with append-only history
    # a PDF elided once is elided on every later pass too.
    to_elide = [entry for entry in found if entry["pages"] is None]
    counted = [entry for entry in found if entry["pages"] is not None]
    counted_total = sum(entry["pages"] for entry in counted)
    for entry in sorted(counted, key=lambda e: e["pages"], reverse=True):
        if counted_total <= MAX_RESEND_PDF_PAGES:
            break
        to_elide.append(entry)
        counted_total -= entry["pages"]
    if not to_elide:
        return messages

    # Pass 3: elide in copies of the affected fetch blocks only.
    rebuilt: dict[int, list] = {}  # msg_idx -> new content list
    elided: list[tuple[int, int]] = []  # (msg_idx, block_idx)
    for entry in sorted(to_elide, key=lambda e: (e["msg_idx"], e["block_idx"])):
        msg_idx, block_idx = entry["msg_idx"], entry["block_idx"]
        if msg_idx not in rebuilt:
            rebuilt[msg_idx] = list(_get(messages[msg_idx], "content") or [])
        block = _to_plain_block(rebuilt[msg_idx][block_idx])
        located = _find_pdf_sources([block])
        if not located or not isinstance(located[0][1], dict):
            continue  # unconvertible block: its PDF stays (defensive)
        _elide_source(located[0][1], pages=entry["pages"])
        rebuilt[msg_idx][block_idx] = block
        elided.append((msg_idx, block_idx))
    if not elided:
        return messages

    # Pass 4: preserved thinking. Every thinking block after the earliest
    # elided PDF was produced with that PDF before it, so it goes.
    return _remove_thinking_after(messages, rebuilt, cut=min(elided))


def _remove_thinking_after(
    messages: list[dict], rebuilt: dict[int, list], *, cut: tuple[int, int]
) -> list[dict]:
    """Assemble the sanitized list without the thinking blocks after ``cut``.

    ``cut`` is the ``(message index, block index)`` of the earliest elided
    PDF; ``rebuilt`` holds the edited content of the messages whose PDFs were
    elided. A thinking or redacted-thinking block of an assistant message
    after that position is removed; the blocks before it keep their prefix,
    so the kept blocks are an unbroken run from the start. An assistant
    message that loses every block is dropped (the API rejects empty
    content). Messages with nothing to change are kept as the same object.
    """
    sanitized: list[dict] = []
    for msg_idx, message in enumerate(messages):
        content = rebuilt.get(msg_idx)
        if msg_idx >= cut[0] and _get(message, "role") == "assistant":
            blocks = content if content is not None else _get(message, "content")
            if isinstance(blocks, (list, tuple)):
                kept = [
                    block
                    for block_idx, block in enumerate(blocks)
                    if (msg_idx, block_idx) <= cut
                    or _get(block, "type") not in _THINKING_TYPES
                ]
                if len(kept) != len(blocks):
                    if not kept:
                        continue
                    content = kept
        if content is None:
            sanitized.append(message)
        else:
            sanitized.append({"role": "assistant", "content": content})
    return sanitized
