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

Eliding also edits history that later thinking blocks were produced after,
which preserved thinking (Opus 5.5 / Sonnet 5.5) rejects, so an eliding pass
removes every thinking block after the earliest PDF it elides. Those tests
run whole conversations — resumed the way the real-time loops resume (the
sanitized list carried forward) and the way the batch builder does (the
original blocks re-sanitized every wave) — through
``tests.fixtures.preserved_thinking``, a model of the API's check.

Hermetic — PDFs are generated in-memory with pypdf.
"""
from __future__ import annotations

import base64
import copy
import functools
import io
import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from pypdf import PdfWriter

from src.core import resend_sanitizer as RS
from src.core.resend_sanitizer import (
    MAX_RESEND_PDF_PAGES,
    sanitize_messages_for_resend,
)
from tests.fixtures.fake_anthropic import FakeThinkingBlock
from tests.fixtures.preserved_thinking import preserved_thinking_violations


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



# ---------------------------------------------------------------------------
# Preserved thinking: blocks, and a model of the API's check
# ---------------------------------------------------------------------------

BIG = MAX_RESEND_PDF_PAGES + 1
_GARBAGE_PDF = base64.b64encode(b"not a pdf at all").decode("ascii")
SYSTEM = [{"type": "text", "text": "you research", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
TOOLS = [{"type": "web_search_20260209", "name": "web_search"}]


def _thinking(signature: str) -> dict:
    return {"type": "thinking", "thinking": f"reasoning {signature}", "signature": signature}


def _redacted(data: str) -> dict:
    return {"type": "redacted_thinking", "data": data}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _search(tag: str) -> dict:
    return {
        "type": "server_tool_use",
        "id": f"srvtoolu_{tag}",
        "name": "web_search",
        "input": {"query": f"query {tag}"},
    }


def _block_field(block: Any, name: str) -> Any:
    return block.get(name) if isinstance(block, dict) else getattr(block, name, None)


def _thinking_in(messages: list) -> list[str]:
    """Signatures (``data`` for a redacted block) of every thinking block, in order."""
    keys = []
    for message in messages:
        content = _block_field(message, "content")
        if isinstance(content, str):
            continue
        for block in content:
            kind = _block_field(block, "type")
            if kind == "thinking":
                keys.append(_block_field(block, "signature"))
            elif kind == "redacted_thinking":
                keys.append(_block_field(block, "data"))
    return keys


def _pdf_pages_in(messages: list) -> int:
    return sum(RS._pdf_page_count(source["data"]) or 0 for source in _pdf_sources_in(messages))


def _body(messages: list, *, system=SYSTEM, tools=TOOLS) -> dict:
    return {"system": system, "tools": tools, "messages": copy.deepcopy(messages)}


class TestPreservedThinkingModel:
    """The model rejects what the documentation says the API rejects.

    Without these, a scenario test passing against the model would say
    nothing: a model that accepts everything passes every scenario.
    """

    RESP0 = [_thinking("t1"), _text("a"), _thinking("t2"), _search("s0")]

    def _first_resume(self, replayed: list, **body_overrides) -> list[str]:
        return preserved_thinking_violations(
            [
                (_body([_user()]), self.RESP0),
                (_body([_user(), _assistant(replayed)], **body_overrides), None),
            ]
        )

    def test_an_append_only_conversation_is_valid(self):
        resp1 = [_thinking("t3"), _text("b")]
        exchanges = [
            (_body([_user()]), self.RESP0),
            (_body([_user(), _assistant(self.RESP0)]), resp1),
            (_body([_user(), _assistant(self.RESP0), _assistant(resp1)]), None),
        ]
        assert preserved_thinking_violations(exchanges) == []

    def test_an_edit_before_a_replayed_block_invalidates_it(self):
        edited = [_thinking("t1"), _text("A, edited"), _thinking("t2"), _search("s0")]
        problems = self._first_resume(edited)
        assert len(problems) == 1
        assert "'t2'" in problems[0] and "changed" in problems[0]

    def test_an_edit_inside_the_producing_response_is_caught_at_its_first_replay(self):
        # The elision case: the PDF is shortened in the very request that
        # first replays the thinking that followed it. A check that read a
        # block's prefix off its first replay would record the edited
        # prefix as the original one and see nothing.
        produced = [_thinking("t1"), _fetch_block(_pdf_b64(BIG)), _thinking("t2")]
        replayed = copy.deepcopy(produced)
        replayed[1]["content"]["content"]["source"] = {"type": "text", "data": "elided"}
        problems = preserved_thinking_violations(
            [
                (_body([_user()]), produced),
                (_body([_user(), _assistant(replayed)]), None),
            ]
        )
        assert len(problems) == 1 and "'t2'" in problems[0]

    @pytest.mark.parametrize(
        "kept",
        [["t2"], ["t1"], []],
        ids=["from-the-start", "from-the-end", "all"],
    )
    def test_removing_blocks_from_the_start_the_end_or_all_is_valid(self, kept):
        replayed = [b for b in self.RESP0 if b["type"] != "thinking" or b["signature"] in kept]
        assert self._first_resume(replayed) == []

    def test_a_gap_is_invalid(self):
        resp0 = [_thinking("t1"), _text("a"), _thinking("t2"), _text("b"), _thinking("t3")]
        replayed = [b for b in resp0 if b.get("signature") != "t2"]
        problems = preserved_thinking_violations(
            [(_body([_user()]), resp0), (_body([_user(), _assistant(replayed)]), None)]
        )
        assert len(problems) == 1 and "'t3'" in problems[0] and "gap" in problems[0]

    def test_a_removed_block_sent_again_is_invalid(self):
        without_t2 = [b for b in self.RESP0 if b.get("signature") != "t2"]
        resp1 = [_text("b")]
        problems = preserved_thinking_violations(
            [
                (_body([_user()]), self.RESP0),
                (_body([_user(), _assistant(without_t2)]), resp1),
                (_body([_user(), _assistant(self.RESP0), _assistant(resp1)]), None),
            ]
        )
        assert problems and all("'t2'" in p for p in problems)
        assert any("sent again" in p for p in problems)

    @pytest.mark.parametrize("field_name", ["system", "tools"])
    def test_a_changed_system_prompt_or_tool_set_invalidates_every_block(self, field_name):
        changed = {field_name: [{"type": "text", "text": "something else"}]}
        problems = self._first_resume(self.RESP0, **changed)
        assert len(problems) == 2

    def test_a_block_no_response_produced_is_invalid(self):
        problems = preserved_thinking_violations(
            [(_body([_user(), _assistant([_thinking("forged"), _text("x")])]), None)]
        )
        assert len(problems) == 1 and "not produced" in problems[0]


# ---------------------------------------------------------------------------
# Preserved thinking: what one eliding pass removes
# ---------------------------------------------------------------------------


class TestThinkingAfterElidedPdf:
    def test_thinking_after_the_pdf_in_its_own_message_is_removed(self):
        t1, t2, tail = _thinking("t1"), _thinking("t2"), _text("tail")
        messages = [_user(), _assistant([t1, _fetch_block(_pdf_b64(BIG)), t2, tail])]
        sanitized = sanitize_messages_for_resend(messages)

        content = sanitized[1]["content"]
        assert [b["type"] for b in content] == ["thinking", "web_fetch_tool_result", "text"]
        assert _pdf_sources_in(sanitized) == []
        # Blocks that did not change are the objects that were sent before.
        assert content[0] is t1 and content[2] is tail

    def test_thinking_in_every_later_assistant_message_is_removed(self):
        messages = [
            _user(),
            _assistant([_thinking("t1"), _fetch_block(_pdf_b64(BIG)), _thinking("t2")]),
            _assistant([_redacted("r1"), _thinking("t3"), _text("c"), _search("s1")]),
        ]
        sanitized = sanitize_messages_for_resend(messages)
        assert _thinking_in(sanitized) == ["t1"]
        assert [b["type"] for b in sanitized[2]["content"]] == ["text", "server_tool_use"]

    def test_messages_before_the_pdf_are_kept_as_they_were(self):
        before = _assistant([_thinking("t0"), _search("s0")])
        messages = [_user(), before, _assistant([_fetch_block(_pdf_b64(BIG)), _thinking("t1")])]
        sanitized = sanitize_messages_for_resend(messages)
        assert sanitized[0] is messages[0] and sanitized[1] is before
        assert _thinking_in(sanitized) == ["t0"]

    def test_an_assistant_message_left_empty_is_dropped(self):
        messages = [
            _user(),
            _assistant([_thinking("t1"), _fetch_block(_pdf_b64(BIG))]),
            _assistant([_thinking("t2"), _redacted("r2")]),
            _assistant([_thinking("t3"), _text("done")]),
        ]
        sanitized = sanitize_messages_for_resend(messages)
        assert [m["role"] for m in sanitized] == ["user", "assistant", "assistant"]
        assert sanitized[2]["content"] == [_text("done")]
        assert _thinking_in(sanitized) == ["t1"]

    def test_an_assistant_message_that_was_already_empty_is_kept(self):
        empty = _assistant([])
        messages = [_user(), _assistant([_fetch_block(_pdf_b64(BIG))]), empty]
        sanitized = sanitize_messages_for_resend(messages)
        assert sanitized[2] is empty

    def test_the_earliest_elided_pdf_sets_the_cut(self):
        # Both PDFs are elided (un-countable first, then the 700-page one):
        # the cut is at the first of them in document order.
        messages = [
            _user(),
            _assistant([_thinking("t1"), _fetch_block(_GARBAGE_PDF), _thinking("t2")]),
            _assistant([_thinking("t3"), _fetch_block(_pdf_b64(700)), _thinking("t4")]),
        ]
        assert _thinking_in(sanitize_messages_for_resend(messages)) == ["t1"]

    def test_a_pdf_that_is_kept_does_not_set_the_cut(self):
        # 300 + 400 > 600: only the larger, later PDF is elided, so the
        # thinking around the earlier (kept) one keeps its prefix.
        small = _pdf_b64(300)
        messages = [
            _user(),
            _assistant([_thinking("t1"), _fetch_block(small), _thinking("t2")]),
            _assistant([_thinking("t3"), _fetch_block(_pdf_b64(400)), _thinking("t4")]),
        ]
        sanitized = sanitize_messages_for_resend(messages)
        assert _thinking_in(sanitized) == ["t1", "t2", "t3"]
        assert [s["data"] for s in _pdf_sources_in(sanitized)] == [small]
        assert sanitized[1] is messages[1]

    def test_object_shaped_thinking_blocks_are_removed(self):
        before = FakeThinkingBlock(thinking="before", signature="t1")
        after = FakeThinkingBlock(thinking="after", signature="t2")
        messages = [_user(), _assistant([before, _fetch_block(_pdf_b64(BIG)), after])]
        content = sanitize_messages_for_resend(messages)[1]["content"]
        assert content[0] is before and len(content) == 2

    def test_a_pdf_that_could_not_be_elided_removes_no_thinking(self):
        # A block the sanitizer cannot copy keeps its PDF (defensive), so
        # nothing before the thinking changed and none of it may go.
        from types import SimpleNamespace

        source = SimpleNamespace(type="base64", media_type="application/pdf", data=_pdf_b64(BIG))
        opaque = SimpleNamespace(
            type="web_fetch_tool_result",
            content=SimpleNamespace(content=SimpleNamespace(source=source)),
        )
        messages = [_user(), _assistant([_thinking("t1"), opaque, _thinking("t2")])]
        assert sanitize_messages_for_resend(messages) is messages

    def test_no_elision_keeps_every_thinking_block_and_the_same_list(self):
        messages = [
            _user(),
            _assistant([_thinking("t1"), _fetch_block(_pdf_b64(3)), _thinking("t2")]),
        ]
        assert sanitize_messages_for_resend(messages) is messages

    def test_the_originals_are_never_mutated(self):
        messages = [
            _user(),
            _assistant([_thinking("t1"), _fetch_block(_pdf_b64(BIG)), _thinking("t2")]),
            _assistant([_thinking("t3")]),
        ]
        snapshot = json.dumps(messages, sort_keys=True)
        sanitize_messages_for_resend(messages)
        assert json.dumps(messages, sort_keys=True) == snapshot


# ---------------------------------------------------------------------------
# Preserved thinking: whole conversations, resumed as each caller resumes
# ---------------------------------------------------------------------------


def _pdf_fetch(pages: int, tag: str) -> dict:
    return _fetch_block(_pdf_b64(pages), url=f"https://codes.example.gov/{tag}.pdf")


def _first_elision_scenario() -> list[list]:
    """Five responses; the second's fetch pushes the total past the limit."""
    return [
        [_thinking("r0a"), _search("s0"), _pdf_fetch(400, "a"), _thinking("r0b"), _search("s0b")],
        [_thinking("r1a"), _pdf_fetch(300, "b"), _thinking("r1b"), _search("s1")],
        [_thinking("r2a"), _text("interim"), _redacted("r2b"), _search("s2")],
        [_thinking("r3a"), _pdf_fetch(350, "c"), _thinking("r3b"), _search("s3")],
        [_thinking("r4a"), _text("done")],
    ]


def _cut_moves_earlier_scenario() -> list[list]:
    """The first eliding pass cuts at a later PDF; a later pass at an earlier one.

    B (400 pages) is elided at the second resume. By the fourth response the
    250-page PDFs alone exceed the limit, and the earliest of them, A, is
    elided too — before B, so the cut moves back past blocks already sent.
    """
    return [
        [_thinking("q0a"), _pdf_fetch(250, "a"), _thinking("q0b"), _search("s0")],
        [_thinking("q1a"), _pdf_fetch(400, "b"), _thinking("q1b"), _search("s1")],
        [_thinking("q2a"), _pdf_fetch(250, "c"), _thinking("q2b"), _search("s2")],
        [_thinking("q3a"), _pdf_fetch(250, "d"), _thinking("q3b"), _search("s3")],
        [_thinking("q4a"), _text("done")],
    ]


SCENARIOS = {
    "first-elision": _first_elision_scenario,
    "cut-moves-earlier": _cut_moves_earlier_scenario,
}


def _resume_realtime(responses: list[list]) -> list[tuple[dict, list]]:
    """Resume as the research and verifier real-time loops do: append the
    paused response, sanitize, and carry the sanitized list forward."""
    messages: list = [_user()]
    exchanges = []
    for index, response in enumerate(responses):
        exchanges.append((_body(messages), response))
        if index == len(responses) - 1:
            break
        messages.append(_assistant(response))
        messages = sanitize_messages_for_resend(messages)
    return exchanges


def _batch_decision():
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


def _resume_batch(responses: list[list]) -> list[tuple[dict, list]]:
    """Resume as the batch wave loop does: every wave re-sends the original
    blocks of every earlier wave, through the real continuation builder."""
    from src.verification.verification_routing import build_verification_request

    decision = _batch_decision()
    accumulated: list = []
    exchanges = []
    for index, response in enumerate(responses):
        request = build_verification_request(
            decision,
            prompt="verify this",
            system_prompt="you verify",
            assistant_content=list(accumulated) if index else None,
            include_service_tier=True,
        )
        exchanges.append((copy.deepcopy(request.params), response))
        accumulated.extend(copy.deepcopy(response))
    return exchanges


RESUMERS = {"realtime": _resume_realtime, "batch": _resume_batch}


def _sent_thinking(exchanges) -> list[list[str]]:
    return [_thinking_in(body["messages"]) for body, _response in exchanges]


class TestRepeatedResumes:
    @pytest.mark.parametrize("scenario", SCENARIOS)
    @pytest.mark.parametrize("resumer", RESUMERS)
    def test_every_request_fits_and_passes_the_prefix_check(self, resumer, scenario):
        exchanges = RESUMERS[resumer](SCENARIOS[scenario]())
        assert all(_pdf_pages_in(body["messages"]) <= MAX_RESEND_PDF_PAGES for body, _ in exchanges)
        assert preserved_thinking_violations(exchanges) == []

    @pytest.mark.parametrize("scenario", SCENARIOS)
    @pytest.mark.parametrize("resumer", RESUMERS)
    def test_eliding_without_removing_thinking_fails_the_check(self, resumer, scenario, monkeypatch):
        # Control: the scenarios do exercise the check. Elision alone (the
        # sanitizer before preserved thinking) replays invalid blocks.
        monkeypatch.setattr(RS, "_THINKING_TYPES", frozenset())
        exchanges = RESUMERS[resumer](SCENARIOS[scenario]())
        assert preserved_thinking_violations(exchanges) != []

    @pytest.mark.parametrize("scenario", SCENARIOS)
    @pytest.mark.parametrize("resumer", RESUMERS)
    def test_a_removed_block_is_never_sent_again(self, resumer, scenario):
        sent = _sent_thinking(RESUMERS[resumer](SCENARIOS[scenario]()))
        removed: set[str] = set()
        for earlier, later in zip(sent, sent[1:]):
            assert not removed & set(later)
            removed |= set(earlier) - set(later)

    @pytest.mark.parametrize("resumer", RESUMERS)
    def test_the_first_eliding_resume_keeps_the_run_before_the_pdf(self, resumer):
        sent = _sent_thinking(RESUMERS[resumer](_first_elision_scenario()))
        # Request 1 replays the first response whole; request 2 is the first
        # past the limit and elides A (400 pages, in the first response).
        assert sent[0] == []
        assert sent[1] == ["r0a", "r0b"]
        assert sent[2] == ["r0a"]

    def test_realtime_keeps_thinking_produced_after_the_elision(self):
        # Response 1 was produced with A whole, so its thinking went at the
        # eliding resume. The carried-forward list then holds A elided, so
        # response 2's thinking was produced after the cut and replays with
        # its own prefix, until C pushes the total over again and cuts after
        # r3a.
        sent = _sent_thinking(_resume_realtime(_first_elision_scenario()))
        assert sent[3] == ["r0a", "r2a", "r2b"]
        assert sent[4] == ["r0a", "r2a", "r2b", "r3a"]

    def test_batch_re_derives_the_same_cut_every_wave(self):
        # From the original blocks every wave elides A again (and C once it
        # arrives), so everything after A goes on every wave.
        sent = _sent_thinking(_resume_batch(_first_elision_scenario()))
        assert sent[2:] == [["r0a"], ["r0a"], ["r0a"]]

    @pytest.mark.parametrize("resumer", RESUMERS)
    def test_the_cut_moves_earlier_when_an_earlier_pdf_is_elided(self, resumer):
        sent = _sent_thinking(RESUMERS[resumer](_cut_moves_earlier_scenario()))
        # B elided at request 2: q0b (before B) still replays.
        assert sent[2] == ["q0a", "q0b", "q1a"]
        # A elided at request 4: everything after A goes, q0b included.
        assert sent[4] == ["q0a"]


# ---------------------------------------------------------------------------
# Preserved thinking: the real-time verifier loop
# ---------------------------------------------------------------------------


class TestRealtimeVerifierWiring:
    def _run(self, monkeypatch) -> list[tuple[dict, list]]:
        from tests.fixtures import verification_drivers as D

        responses = [
            D.message(
                [
                    _thinking("v1"),
                    _search("fetch"),
                    _pdf_fetch(BIG, "full-code"),
                    _thinking("v2"),
                    *D.search_blocks(),
                ],
                stop_reason="pause_turn",
            ),
            D.message(
                [_thinking("v3"), D.verdict_call(D.verdict_payload())],
                stop_reason="tool_use",
            ),
        ]
        requests: list[dict] = []

        def route(kwargs):
            # The loop keeps appending to the list it sent: snapshot it now.
            requests.append(copy.deepcopy(kwargs))
            return responses[len(requests) - 1]

        D.run_realtime(monkeypatch, route)
        assert len(requests) == 2
        return [(request, response.content) for request, response in zip(requests, responses)]

    def test_the_eliding_resume_passes_the_prefix_check(self, monkeypatch):
        exchanges = self._run(monkeypatch)
        resumed = exchanges[1][0]["messages"]
        assert _pdf_sources_in(resumed) == []
        assert _thinking_in(resumed) == ["v1"]
        assert preserved_thinking_violations(exchanges) == []

    def test_control_eliding_alone_replays_an_invalid_block(self, monkeypatch):
        monkeypatch.setattr(RS, "_THINKING_TYPES", frozenset())
        assert preserved_thinking_violations(self._run(monkeypatch)) != []
