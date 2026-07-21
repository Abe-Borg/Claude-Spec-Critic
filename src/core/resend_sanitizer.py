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


def _get(node: Any, key: str) -> Any:
    """Read ``key`` from a dict or an attribute from an SDK/dataclass object."""
    if isinstance(node, dict):
        return node.get(key)
    return getattr(node, key, None)


def _find_pdf_sources(content: Any) -> list[Any]:
    """Return the base64-PDF ``source`` nodes inside fetched documents.

    Targeted traversal (mirrors ``verifier._collect_fetch_evidence_detailed``
    rather than a blind recursive walk): assistant content can only carry
    PDFs inside ``web_fetch_tool_result`` blocks, whose single fetched
    document sits at ``block.content.content`` (current SDK) or
    ``block.content.document`` (older echo shape). Document order.
    """
    sources: list[Any] = []
    for block in content or []:
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
            sources.append(source)
    return sources


def _pdf_page_count(b64_data: Any) -> int | None:
    """Count a base64 PDF's pages; ``None`` when it cannot be determined."""
    try:
        from pypdf import PdfReader

        raw = base64.b64decode(b64_data)
        return len(PdfReader(io.BytesIO(raw)).pages)
    except Exception:  # noqa: BLE001 — un-countable is a valid outcome
        return None


def to_plain_block(block: Any) -> Any:
    """Best-effort conversion of a content block to a mutable plain dict.

    Dicts are deep-copied (the caller mutates the copy, never the
    original response object — traces and evidence collectors keep reading
    pristine data). SDK pydantic models dump to JSON-mode dicts; dataclass
    fixtures convert via ``asdict``. An unconvertible block is returned
    as-is and its PDFs simply stay un-elided (defensive: no crash).

    Public: also consumed by ``core.continuation_cache`` for the
    copy-on-write message-level cache breakpoint.
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


# Backwards-compatible private alias (pre-promotion name).
_to_plain_block = to_plain_block


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
    countable one — with a plain-text note until the remainder fits.

    Returns ``messages`` unchanged (same object) when nothing needs
    eliding, so the common no-PDF path stays byte-identical. When eliding,
    returns a new list in which only the affected messages are rebuilt
    (deep-copied / dumped to plain dicts); the input and the underlying
    response objects are never mutated.
    """
    # Pass 1: locate + count every fetched PDF, reading originals in place.
    found: list[dict[str, Any]] = []  # {msg_idx, source_idx, pages}
    for msg_idx, message in enumerate(messages):
        if _get(message, "role") != "assistant":
            continue
        for source_idx, source in enumerate(_find_pdf_sources(_get(message, "content"))):
            found.append(
                {
                    "msg_idx": msg_idx,
                    "source_idx": source_idx,
                    "pages": _pdf_page_count(_get(source, "data")),
                }
            )
    if not found:
        return messages

    # Pass 2: decide which PDFs to elide. Un-countable PDFs go first (they
    # cannot be trusted against the ceiling); then largest-first among the
    # counted until the counted total fits under the limit.
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

    # Pass 3: rebuild only the affected messages and elide in the copies.
    elide_by_msg: dict[int, dict[int, int | None]] = {}
    for entry in to_elide:
        elide_by_msg.setdefault(entry["msg_idx"], {})[entry["source_idx"]] = entry["pages"]

    sanitized = list(messages)
    for msg_idx, source_map in elide_by_msg.items():
        original = messages[msg_idx]
        new_content = [to_plain_block(b) for b in (_get(original, "content") or [])]
        for source_idx, source in enumerate(_find_pdf_sources(new_content)):
            if source_idx in source_map and isinstance(source, dict):
                _elide_source(source, pages=source_map[source_idx])
        sanitized[msg_idx] = {"role": "assistant", "content": new_content}
    return sanitized
