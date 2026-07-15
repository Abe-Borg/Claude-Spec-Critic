"""Continuation-resume PDF sanitizer (``core/resend_sanitizer.py``).

The Messages API enforces its per-request PDF page limit on *inbound*
content, including ``web_fetch`` documents the API itself produced — so a
pause_turn resume that re-sends a fetched >600-page PDF is rejected with
HTTP 400 (observed live: a research dimension fetched a full building code
and its continuation died with ``messages.1.content.22.pdf.source.base64.
data: A maximum of 600 PDF pages may be provided``). These tests pin the
sanitizer's policy: byte-identical no-op for the common path, elision of
oversized / un-countable fetched PDFs, no mutation of the originals, and
wiring through the batch continuation builder.

Hermetic — PDFs are generated in-memory with pypdf.
"""
from __future__ import annotations

import base64
import functools
import io
import json
from dataclasses import dataclass, field
from typing import Any

from pypdf import PdfWriter

from src.core.resend_sanitizer import (
    MAX_RESEND_PDF_PAGES,
    sanitize_messages_for_resend,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def _pdf_b64(pages: int) -> str:
    """Base64 of an in-memory blank PDF with the given page count."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _fetch_block(data_b64: str, url: str = "https://codes.example.gov/full-code.pdf") -> dict:
    """A dict-shaped ``web_fetch_tool_result`` block (batch retrieval shape)."""
    return {
        "type": "web_fetch_tool_result",
        "tool_use_id": "srvtoolu_fetch_1",
        "content": {
            "type": "web_fetch_result",
            "url": url,
            "content": {
                "type": "document",
                "title": "Fetched PDF",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": data_b64,
                },
            },
            "retrieved_at": "2026-07-15T00:00:00Z",
        },
    }


def _assistant(content: list) -> dict:
    return {"role": "assistant", "content": content}


def _user(text: str = "verify this") -> dict:
    return {"role": "user", "content": text}


def _pdf_sources_in(messages: list) -> list[dict]:
    """All base64/PDF source dicts anywhere in a JSON-serializable tree."""
    found: list[dict] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if (
                node.get("type") == "base64"
                and node.get("media_type") == "application/pdf"
            ):
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(messages)
    return found


# ---------------------------------------------------------------------------
# Identity paths (the dominant case must stay byte-identical)
# ---------------------------------------------------------------------------


class TestIdentityPaths:
    def test_no_pdfs_returns_same_object(self):
        messages = [
            _user(),
            _assistant([{"type": "text", "text": "searching..."}]),
        ]
        assert sanitize_messages_for_resend(messages) is messages

    def test_pdfs_under_limit_return_same_object(self):
        messages = [_user(), _assistant([_fetch_block(_pdf_b64(3))])]
        assert sanitize_messages_for_resend(messages) is messages

    def test_total_exactly_at_limit_is_kept(self):
        # Strict '>' — a request at exactly the ceiling is valid.
        messages = [
            _user(),
            _assistant(
                [
                    _fetch_block(_pdf_b64(MAX_RESEND_PDF_PAGES - 4)),
                    _fetch_block(_pdf_b64(4)),
                ]
            ),
        ]
        assert sanitize_messages_for_resend(messages) is messages

    def test_pdf_in_user_message_is_out_of_scope(self):
        # Fetched documents only ever appear in assistant turns; a
        # user-message document is not the resend problem and is left alone.
        messages = [
            {"role": "user", "content": [_fetch_block(_pdf_b64(2))]},
        ]
        assert sanitize_messages_for_resend(messages) is messages


# ---------------------------------------------------------------------------
# Elision policy
# ---------------------------------------------------------------------------


class TestElision:
    def test_over_limit_pdf_is_elided(self):
        big = MAX_RESEND_PDF_PAGES + 1
        messages = [_user(), _assistant([_fetch_block(_pdf_b64(big))])]
        sanitized = sanitize_messages_for_resend(messages)

        assert sanitized is not messages
        assert _pdf_sources_in(sanitized) == []
        source = sanitized[1]["content"][0]["content"]["content"]["source"]
        assert source["type"] == "text"
        assert source["media_type"] == "text/plain"
        assert f"{big} pages" in source["data"]
        assert str(MAX_RESEND_PDF_PAGES) in source["data"]

    def test_originals_are_never_mutated(self):
        messages = [_user(), _assistant([_fetch_block(_pdf_b64(MAX_RESEND_PDF_PAGES + 1))])]
        snapshot = json.dumps(messages, sort_keys=True)
        sanitize_messages_for_resend(messages)
        assert json.dumps(messages, sort_keys=True) == snapshot

    def test_surrounding_block_fields_survive_elision(self):
        messages = [_user(), _assistant([_fetch_block(_pdf_b64(MAX_RESEND_PDF_PAGES + 1))])]
        sanitized = sanitize_messages_for_resend(messages)
        block = sanitized[1]["content"][0]
        assert block["tool_use_id"] == "srvtoolu_fetch_1"
        assert block["content"]["url"] == "https://codes.example.gov/full-code.pdf"
        assert block["content"]["content"]["title"] == "Fetched PDF"

    def test_limit_is_total_across_pdfs_largest_elided_first(self):
        # 400 + 300 = 700 > 600: only the larger one needs to go.
        messages = [
            _user(),
            _assistant([_fetch_block(_pdf_b64(400)), _fetch_block(_pdf_b64(300))]),
        ]
        sanitized = sanitize_messages_for_resend(messages)
        remaining = _pdf_sources_in(sanitized)
        assert len(remaining) == 1
        # The smaller PDF (300 pages) is the survivor.
        assert remaining[0]["data"] == _pdf_b64(300)

    def test_limit_spans_multiple_assistant_messages(self):
        # Two continuation turns, each with a fits-alone PDF whose sum
        # exceeds the per-request ceiling — the request carries both.
        messages = [
            _user(),
            _assistant([_fetch_block(_pdf_b64(400))]),
            _assistant([_fetch_block(_pdf_b64(350))]),
        ]
        sanitized = sanitize_messages_for_resend(messages)
        remaining = _pdf_sources_in(sanitized)
        assert len(remaining) == 1
        assert remaining[0]["data"] == _pdf_b64(350)

    def test_unparseable_pdf_is_elided(self):
        garbage = base64.b64encode(b"not a pdf at all").decode("ascii")
        messages = [_user(), _assistant([_fetch_block(garbage)])]
        sanitized = sanitize_messages_for_resend(messages)
        assert _pdf_sources_in(sanitized) == []
        source = sanitized[1]["content"][0]["content"]["content"]["source"]
        assert "could not be determined" in source["data"]

    def test_non_pdf_documents_are_ignored(self):
        block = _fetch_block(_pdf_b64(2))
        block["content"]["content"]["source"] = {
            "type": "text",
            "media_type": "text/plain",
            "data": "plain fetched page",
        }
        messages = [_user(), _assistant([block])]
        assert sanitize_messages_for_resend(messages) is messages


# ---------------------------------------------------------------------------
# Block-shape tolerance (SDK objects vs plain dicts)
# ---------------------------------------------------------------------------


@dataclass
class _ObjSource:
    data: str
    type: str = "base64"
    media_type: str = "application/pdf"


@dataclass
class _ObjDocument:
    source: _ObjSource
    type: str = "document"
    title: str = "Fetched PDF"


@dataclass
class _ObjFetchResult:
    content: _ObjDocument
    type: str = "web_fetch_result"
    url: str = "https://codes.example.gov/full-code.pdf"


@dataclass
class _ObjFetchBlock:
    content: _ObjFetchResult
    type: str = "web_fetch_tool_result"
    tool_use_id: str = "srvtoolu_fetch_1"


class TestObjectShapedBlocks:
    def test_object_blocks_are_converted_and_elided(self):
        block = _ObjFetchBlock(
            content=_ObjFetchResult(
                content=_ObjDocument(
                    source=_ObjSource(data=_pdf_b64(MAX_RESEND_PDF_PAGES + 1))
                )
            )
        )
        messages = [_user(), _assistant([block])]
        sanitized = sanitize_messages_for_resend(messages)
        assert _pdf_sources_in(sanitized) == []
        rebuilt = sanitized[1]["content"][0]
        assert isinstance(rebuilt, dict)
        assert rebuilt["content"]["content"]["source"]["type"] == "text"
        # The original object graph is untouched.
        assert block.content.content.source.type == "base64"

    def test_object_blocks_under_limit_identity(self):
        block = _ObjFetchBlock(
            content=_ObjFetchResult(
                content=_ObjDocument(source=_ObjSource(data=_pdf_b64(2)))
            )
        )
        messages = [_user(), _assistant([block])]
        assert sanitize_messages_for_resend(messages) is messages

    def test_legacy_document_key_shape(self):
        # Older SDK echo shape: the fetched document under ``document``
        # instead of ``content``.
        messages = [
            _user(),
            _assistant(
                [
                    {
                        "type": "web_fetch_tool_result",
                        "tool_use_id": "srvtoolu_fetch_1",
                        "content": {
                            "type": "web_fetch_result",
                            "url": "https://codes.example.gov/full-code.pdf",
                            "document": {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": _pdf_b64(MAX_RESEND_PDF_PAGES + 1),
                                },
                            },
                        },
                    }
                ]
            ),
        ]
        sanitized = sanitize_messages_for_resend(messages)
        assert _pdf_sources_in(sanitized) == []


# ---------------------------------------------------------------------------
# Batch continuation builder wiring (verification_routing)
# ---------------------------------------------------------------------------


class TestBatchContinuationWiring:
    def _decision(self):
        from src.review.reviewer import Finding
        from src.verification.verification_routing import select_routing

        finding = Finding(
            severity="HIGH",
            fileName="Section_21_1000.docx",
            section="2.1",
            issue="NFPA 13 spacing requirement",
            actionType="EDIT",
            existingText="max spacing 12 ft",
            replacementText="max spacing 15 ft",
            codeReference="NFPA 13 §10.2.5",
            confidence=0.7,
        )
        return select_routing(finding, escalated=False, local_skip=False)

    def test_continuation_request_elides_oversized_fetched_pdf(self):
        from src.verification.verification_routing import build_verification_request

        request = build_verification_request(
            self._decision(),
            prompt="verify this",
            system_prompt="you verify",
            assistant_content=[_fetch_block(_pdf_b64(MAX_RESEND_PDF_PAGES + 1))],
        )
        messages = request.params["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert _pdf_sources_in(messages) == []

    def test_continuation_request_keeps_small_fetched_pdf(self):
        from src.verification.verification_routing import build_verification_request

        assistant_content = [_fetch_block(_pdf_b64(2))]
        request = build_verification_request(
            self._decision(),
            prompt="verify this",
            system_prompt="you verify",
            assistant_content=assistant_content,
        )
        # Under the limit: the assistant content rides through untouched.
        assert request.params["messages"][1]["content"] is assistant_content
