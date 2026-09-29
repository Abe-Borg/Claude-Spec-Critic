"""Native citations: captured, resolved honestly, cached, and displayed (plan WP-16).

The Messages API attaches its own citations to the verifier's text — a
``web_search_result_location`` naming a search result, or a
``char_location`` / ``page_location`` / ``content_block_location`` pointing
into a fetched document by index. Until S17 every one was discarded. These
tests pin the contract that replaced that:

* **Capture** on both transports, from the same response, with the tool,
  source, cited text, locator, and the attempt and model that produced it.
* **Resolution** of a document index only through the documents *this*
  conversation fetched before the citation, and never to a nearby URL when
  the mapping is contradicted or unknown.
* **Unknown shapes** stay visible without discarding the result.
* **The cache** round-trips the optional fields, labels legacy rows "not
  captured", and never persists a fetched document.
* **Three concepts, kept apart** — retrieval, native attribution, and
  semantic support — in the trace and in both reports.
* **Acceptance is unchanged**: a citation never grounds, demotes, or caches
  a verdict, and no text-overlap threshold decides anything.
* **Fetch instructions** follow the provider's rule (any URL already in the
  conversation), keep the support / edition / authority / applicability
  checks, and leave the capability gates alone.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import (
    Base64PDFSource,
    CitationCharLocation,
    CitationPageLocation,
    CitationsWebSearchResultLocation,
    DocumentBlock,
    PlainTextSource,
    TextBlock,
    WebFetchBlock,
    WebFetchToolResultBlock,
    WebFetchToolResultErrorBlock,
)
from docx import Document

import src.verification.verifier as V
from src.core.code_cycles import DEFAULT_CYCLE
from src.modules.registry import AVAILABLE_MODULES
from src.output import report_exporter as RE
from src.output import html_report_exporter as HE
from src.tracing import capture_hooks
from src.verification import native_citations as N
from src.verification.verification_cache import (
    VerificationCache,
    cache_ineligibility_reason,
)
from src.verification.verifier import VerificationResult
from tests.fixtures.fake_anthropic import FakeServerToolUseBlock, FakeTextBlock
from tests.fixtures.verification_drivers import (
    QUOTE,
    SEARCHED_URL,
    medium_finding,
    message,
    run_batch,
    run_realtime,
    search_blocks,
    verdict_call,
    verdict_payload,
)

FETCH_URL = "https://codes.example.gov/fire/nfpa-13-adoption"
PDF_URL = "https://codes.example.gov/fire/nfpa-13.pdf"
DOC_TEXT = (
    "Chapter 10 Installation. Sprinklers shall be spaced not more than 15 ft "
    "apart for light hazard occupancies. Exceptions apply to small rooms."
)
DOC_SENTENCE = "Sprinklers shall be spaced not more than 15 ft apart for light hazard occupancies."
SEARCH_CITED = "The maximum distance between sprinklers shall not exceed 15 ft."


# ---------------------------------------------------------------------------
# Builders — real SDK types, so the reader is exercised on the shapes the
# streaming path actually hands it.
# ---------------------------------------------------------------------------


def search_citation(url: str = SEARCHED_URL, text: str = SEARCH_CITED) -> CitationsWebSearchResultLocation:
    return CitationsWebSearchResultLocation(
        type="web_search_result_location",
        url=url,
        title="NFPA 13",
        cited_text=text,
        encrypted_index="opaque-index-token",
    )


def char_citation(index: int = 0, text: str = DOC_SENTENCE, title: str | None = "NFPA 13 adoption") -> CitationCharLocation:
    return CitationCharLocation(
        type="char_location",
        document_index=index,
        document_title=title,
        start_char_index=32,
        end_char_index=32 + len(text),
        cited_text=text,
    )


def page_citation(index: int = 0, text: str = "Spacing limits are in Chapter 10.") -> CitationPageLocation:
    return CitationPageLocation(
        type="page_location",
        document_index=index,
        document_title=None,
        start_page_number=12,
        end_page_number=13,
        cited_text=text,
    )


def cited_text_block(*citations, text: str = "The standard sets the limit at 15 ft.") -> TextBlock:
    return TextBlock(type="text", text=text, citations=list(citations))


def fetch_pair(url: str = FETCH_URL, text: str = DOC_TEXT, title: str = "NFPA 13 adoption", tool_id: str = "srvtoolu_f1") -> list:
    return [
        FakeServerToolUseBlock(name="web_fetch", input={"url": url}, id=tool_id),
        WebFetchToolResultBlock(
            type="web_fetch_tool_result",
            tool_use_id=tool_id,
            content=WebFetchBlock(
                type="web_fetch_result",
                url=url,
                retrieved_at="2026-09-29T10:00:00Z",
                content=DocumentBlock(
                    type="document",
                    title=title,
                    source=PlainTextSource(type="text", media_type="text/plain", data=text),
                ),
            ),
        ),
    ]


def pdf_fetch_pair(url: str = PDF_URL, tool_id: str = "srvtoolu_pdf") -> list:
    return [
        FakeServerToolUseBlock(name="web_fetch", input={"url": url}, id=tool_id),
        WebFetchToolResultBlock(
            type="web_fetch_tool_result",
            tool_use_id=tool_id,
            content=WebFetchBlock(
                type="web_fetch_result",
                url=url,
                retrieved_at=None,
                content=DocumentBlock(
                    type="document",
                    title=None,
                    source=Base64PDFSource(type="base64", media_type="application/pdf", data="JVBERi0="),
                ),
            ),
        ),
    ]


def failed_fetch_pair(url: str = "https://dead.example/x", tool_id: str = "srvtoolu_err") -> list:
    return [
        FakeServerToolUseBlock(name="web_fetch", input={"url": url}, id=tool_id),
        WebFetchToolResultBlock(
            type="web_fetch_tool_result",
            tool_use_id=tool_id,
            content=WebFetchToolResultErrorBlock(
                type="web_fetch_tool_result_error", error_code="url_not_accessible"
            ),
        ),
    ]


def conversation(*blocks) -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks))


def cited_verdict_message(**overrides):
    """Search + fetch, text citing both, and a CONFIRMED verdict citing the search URL."""
    blocks = [
        *search_blocks(),
        *fetch_pair(),
        cited_text_block(search_citation(), char_citation()),
        verdict_call(verdict_payload(**overrides)),
    ]
    return message(blocks, fetches=1)


# ---------------------------------------------------------------------------
# Collection and resolution
# ---------------------------------------------------------------------------


class TestCollection:
    def test_a_search_citation_names_its_source_directly(self):
        (record,) = N.collect_native_citations([conversation(cited_text_block(search_citation()))])
        assert record["type"] == N.CITATION_WEB_SEARCH
        assert record["recognized"] is True
        assert record["tool"] == N.TOOL_WEB_SEARCH
        assert record["url"] == SEARCHED_URL
        assert record["title"] == "NFPA 13"
        assert record["cited_text"] == SEARCH_CITED
        assert record["resolution"] == N.RESOLUTION_DIRECT
        # The opaque replay token identifies nothing a reader can use.
        assert "opaque-index-token" not in json.dumps(record)

    def test_a_document_citation_resolves_through_the_fetched_document(self):
        (record,) = N.collect_native_citations(
            [conversation(*fetch_pair(), cited_text_block(char_citation()))]
        )
        assert record["tool"] == N.TOOL_WEB_FETCH
        assert record["url"] == FETCH_URL
        assert record["document_index"] == 0
        assert record["resolution"] == N.RESOLUTION_DOCUMENT_TEXT
        assert record["locator"] == {
            "start_char_index": 32,
            "end_char_index": 32 + len(DOC_SENTENCE),
        }

    def test_a_pdf_citation_resolves_by_position_and_says_so(self):
        (record,) = N.collect_native_citations(
            [conversation(*pdf_fetch_pair(), cited_text_block(page_citation()))]
        )
        assert record["url"] == PDF_URL
        assert record["resolution"] == N.RESOLUTION_DOCUMENT_INDEX
        assert "position only" in record["resolution_note"]
        assert record["locator"] == {"start_page_number": 12, "end_page_number": 13}

    def test_an_index_resolves_only_to_a_document_fetched_before_the_citation(self):
        # Index 1 names a document that exists later in the conversation but
        # had not been fetched when the model wrote the citation. Mapping it
        # there would attach an arbitrary nearby URL.
        records = N.collect_native_citations(
            [
                conversation(*fetch_pair(), cited_text_block(char_citation(index=1))),
                conversation(*fetch_pair(url="https://later.example/doc", title="Later", tool_id="t2")),
            ]
        )
        (record,) = records
        assert record["resolution"] == N.RESOLUTION_UNRESOLVED
        assert record["url"] == ""
        assert record["tool"] == ""
        assert "names no document retrieved before it" in record["resolution_note"]

    def test_an_index_counts_documents_across_the_whole_conversation(self):
        # A resumed turn cites a document an earlier turn fetched.
        second_text = "Exceptions apply to small rooms."
        records = N.collect_native_citations(
            [
                conversation(*fetch_pair()),
                conversation(
                    *fetch_pair(url="https://second.example/doc", text="Other text entirely.", title="Second", tool_id="t2"),
                    cited_text_block(char_citation(index=0, text=second_text)),
                ),
            ]
        )
        (record,) = records
        assert record["url"] == FETCH_URL
        assert record["resolution"] == N.RESOLUTION_DOCUMENT_TEXT

    def test_a_failed_fetch_is_not_a_document(self):
        (record,) = N.collect_native_citations(
            [conversation(*failed_fetch_pair(), *fetch_pair(), cited_text_block(char_citation(index=0)))]
        )
        assert record["url"] == FETCH_URL

    @pytest.mark.parametrize(
        "citation, reason",
        [
            (char_citation(index=5), "names no document retrieved before it"),
            (char_citation(title="A different page"), "different title"),
            (char_citation(text="This sentence is not in the fetched page."), "cited text is not in the document"),
        ],
        ids=["out_of_range", "title_mismatch", "text_absent"],
    )
    def test_a_contradicted_mapping_is_left_unresolved(self, citation, reason):
        (record,) = N.collect_native_citations(
            [conversation(*fetch_pair(), cited_text_block(citation))]
        )
        assert record["resolution"] == N.RESOLUTION_UNRESOLVED
        assert record["url"] == ""
        assert reason in record["resolution_note"]

    def test_the_legacy_document_echo_is_read_for_the_identity_check(self):
        # The older ``content.document`` shape carries the page body as a
        # string; it must be read, not treated as an unreadable page matched
        # by position alone (found in review).
        legacy = {
            "type": "web_fetch_tool_result",
            "tool_use_id": "srvtoolu_legacy",
            "content": {
                "type": "web_fetch_result",
                "url": FETCH_URL,
                "document": {"url": FETCH_URL, "title": "", "content": DOC_TEXT},
            },
        }
        (found,) = N.collect_native_citations(
            [conversation(legacy, cited_text_block(char_citation(title=None)))]
        )
        assert found["resolution"] == N.RESOLUTION_DOCUMENT_TEXT
        assert found["url"] == FETCH_URL
        (absent,) = N.collect_native_citations(
            [conversation(legacy, cited_text_block(char_citation(title=None, text="Not on this page.")))]
        )
        assert absent["resolution"] == N.RESOLUTION_UNRESOLVED
        assert absent["url"] == ""

    def test_a_result_without_a_url_takes_its_own_calls_url(self):
        # A result that does not echo its URL is tied to the ``web_fetch``
        # call with its ``tool_use_id`` — never to another fetch (found in
        # review).
        def result_without_url(tool_use_id: str) -> dict:
            return {
                "type": "web_fetch_tool_result",
                "tool_use_id": tool_use_id,
                "content": {
                    "type": "web_fetch_result",
                    "content": {"type": "document", "source": {"type": "text", "data": DOC_TEXT}},
                },
            }

        other = "https://other.example/page"
        (found,) = N.collect_native_citations(
            [
                conversation(
                    FakeServerToolUseBlock(name="web_fetch", input={"url": other}, id="call_other"),
                    FakeServerToolUseBlock(name="web_fetch", input={"url": FETCH_URL}, id="call_mine"),
                    result_without_url("call_mine"),
                    cited_text_block(char_citation(title=None)),
                )
            ]
        )
        assert found["url"] == FETCH_URL
        assert found["resolution"] == N.RESOLUTION_DOCUMENT_TEXT
        (orphan,) = N.collect_native_citations(
            [conversation(result_without_url("call_unknown"), cited_text_block(char_citation(title=None)))]
        )
        assert orphan["resolution"] == N.RESOLUTION_UNRESOLVED
        assert orphan["url"] == ""
        assert "no URL" in orphan["resolution_note"]

    def test_whitespace_differences_do_not_break_the_identity_check(self):
        spaced = "Sprinklers shall be spaced   not more than\n15 ft apart for light hazard occupancies."
        (record,) = N.collect_native_citations(
            [conversation(*fetch_pair(), cited_text_block(char_citation(text=spaced)))]
        )
        assert record["resolution"] == N.RESOLUTION_DOCUMENT_TEXT

    def test_an_unknown_shape_is_recorded_not_dropped(self):
        unknown = {"type": "video_timestamp_location", "cited_text": "at 01:02", "start_ms": 62000}
        records = N.collect_native_citations(
            [conversation({"type": "text", "text": "x", "citations": [unknown, search_citation()]})]
        )
        assert [r["recognized"] for r in records] == [False, True]
        odd = records[0]
        assert odd["type"] == "video_timestamp_location"
        assert odd["cited_text"] == "at 01:02"
        assert odd["fields"] == ["cited_text", "start_ms", "type"]
        assert odd["url"] == ""
        assert N.unrecognized_count(records) == 1

    def test_a_citations_field_that_is_not_a_list_is_one_unrecognized_record(self):
        (record,) = N.collect_native_citations(
            [conversation({"type": "text", "text": "x", "citations": "oops"})]
        )
        assert record["recognized"] is False
        assert "not a list" in record["resolution_note"]

    def test_plain_dict_blocks_read_like_sdk_objects(self):
        sdk = N.collect_native_citations([conversation(*fetch_pair(), cited_text_block(search_citation(), char_citation()))])
        as_dicts = [
            b.model_dump(mode="json", exclude_none=True) if hasattr(b, "model_dump") else b
            for b in [*fetch_pair(), cited_text_block(search_citation(), char_citation())]
        ]
        plain = N.collect_native_citations([conversation(*as_dicts)])
        assert plain == sdk


class TestBounds:
    def test_cited_text_is_cut_and_flagged(self):
        long_text = "x" * (N.MAX_CITED_TEXT_CHARS + 50)
        (record,) = N.collect_native_citations(
            [conversation(cited_text_block(search_citation(text=long_text)))]
        )
        assert len(record["cited_text"]) == N.MAX_CITED_TEXT_CHARS
        assert record["cited_text_truncated"] is True

    def test_the_cap_keeps_verdict_cited_sources_first_and_counts_the_rest(self):
        citations = [
            search_citation(url=f"https://other.example/{i}", text=f"passage {i}")
            for i in range(N.MAX_NATIVE_CITATIONS + 5)
        ]
        citations.append(search_citation(url=SEARCHED_URL, text="the one the verdict cites"))
        raw = N.collect_native_citations([conversation(cited_text_block(*citations))])
        kept, omitted = N.associate_native_citations(
            raw, retrieved_urls=[SEARCHED_URL], verdict_sources=[SEARCHED_URL]
        )
        assert len(kept) == N.MAX_NATIVE_CITATIONS
        assert omitted == 6
        # Kept despite being last; the kept records stay in conversation order.
        assert kept[-1]["url"] == SEARCHED_URL
        assert kept[-1]["verdict_cites_source"] is True
        assert [r["url"] for r in kept[:-1]] == [
            f"https://other.example/{i}" for i in range(N.MAX_NATIVE_CITATIONS - 1)
        ]

    def test_duplicates_collapse(self):
        raw = N.collect_native_citations(
            [conversation(cited_text_block(search_citation(), search_citation()))]
        )
        kept, omitted = N.associate_native_citations(raw, retrieved_urls=[], verdict_sources=[])
        assert len(kept) == 1 and omitted == 0


# ---------------------------------------------------------------------------
# Both transports
# ---------------------------------------------------------------------------


def _without_transport_identity(records: list[dict]) -> list[dict]:
    return [
        {k: v for k, v in r.items() if k not in ("attempt_id", "transport")}
        for r in records
    ]


class TestTransports:
    def test_both_transports_capture_the_same_citations(self, monkeypatch):
        realtime, _ = run_realtime(monkeypatch, cited_verdict_message())
        batch = run_batch(monkeypatch, cited_verdict_message()).verification
        assert realtime.native_citations and batch.native_citations
        assert _without_transport_identity(realtime.native_citations) == _without_transport_identity(
            batch.native_citations
        )
        search, fetched = realtime.native_citations
        assert search["url"] == SEARCHED_URL and search["retrieved"] is True
        assert search["verdict_cites_source"] is True
        assert fetched["url"] == FETCH_URL and fetched["tool"] == N.TOOL_WEB_FETCH
        assert fetched["retrieved"] is True
        # The verdict cited only the search URL.
        assert fetched["verdict_cites_source"] is False

    def test_each_citation_names_its_attempt_model_and_role(self, monkeypatch):
        realtime, _ = run_realtime(monkeypatch, cited_verdict_message())
        batch = run_batch(monkeypatch, cited_verdict_message()).verification
        for result, transport in ((realtime, "realtime"), (batch, "batch")):
            (kept_attempt,) = [a for a in result.call_usage if a.get("usage_known", True)]
            for record in result.native_citations:
                assert record["model"] == result.model_used
                assert record["role"] == "primary"
                assert record["transport"] == transport
            attempt_ids = {r["attempt_id"] for r in result.native_citations}
            assert attempt_ids and all(attempt_ids)
        assert all(r["attempt_id"].startswith("message:") for r in realtime.native_citations)
        assert {r["attempt_id"] for r in batch.native_citations} == {"batch:init-batch:verify__0:primary"}

    def test_citations_never_change_acceptance(self, monkeypatch):
        """The same verdict with and without native citations is judged the same."""
        plain = message([*search_blocks(), *fetch_pair(), verdict_call(verdict_payload())], fetches=1)
        with_cites, _ = run_realtime(monkeypatch, cited_verdict_message())
        without, _ = run_realtime(monkeypatch, plain)
        for name in ("verdict", "grounded", "sources", "accepted_sources", "rejected_sources", "outcome"):
            assert getattr(with_cites, name) == getattr(without, name), name
        assert cache_ineligibility_reason(with_cites) == cache_ineligibility_reason(without)
        assert without.native_citations == []

    def test_a_native_citation_does_not_stand_in_for_a_verdict_source(self, monkeypatch):
        # The model cites nothing in its verdict; the API's citation to the
        # searched page must not ground the CONFIRMED.
        result, _ = run_realtime(monkeypatch, cited_verdict_message(sources=[]))
        assert result.verdict == "UNVERIFIED"
        assert result.grounded is False
        assert result.sources == []
        assert all(r["verdict_cites_source"] is False for r in result.native_citations)

    def test_only_an_accepted_verdict_source_ties_a_citation_to_the_verdict(self, monkeypatch):
        # The API cites a page the recorded search results do not hold, and
        # the verdict names it too: grounding rejects that verdict source, so
        # the citation is neither "retrieved" nor "cited by the verdict".
        elsewhere = "https://elsewhere.example/p"
        blocks = [
            *search_blocks(),
            cited_text_block(search_citation(url=elsewhere)),
            verdict_call(verdict_payload(sources=[SEARCHED_URL, elsewhere])),
        ]
        result, _ = run_realtime(monkeypatch, message(blocks))
        assert result.accepted_sources == [SEARCHED_URL]
        assert [r["url"] for r in result.rejected_sources] == [elsewhere]
        (record,) = result.native_citations
        assert record["url"] == elsewhere
        assert record["retrieved"] is False
        assert record["verdict_cites_source"] is False

    def test_a_blank_source_is_still_no_source(self, monkeypatch):
        result, _ = run_realtime(monkeypatch, cited_verdict_message(sources=["  "]))
        assert result.verdict == "UNVERIFIED"
        assert cache_ineligibility_reason(result) is not None

    def test_a_citation_after_a_pause_resolves_through_the_earlier_turn(self, monkeypatch):
        first = message([*search_blocks(), *fetch_pair()], stop_reason="pause_turn", fetches=1)
        second = message(
            [cited_text_block(char_citation()), verdict_call(verdict_payload(sources=[FETCH_URL]))],
            searches=0,
        )
        replies = iter([first, second])
        realtime, _ = run_realtime(monkeypatch, lambda _kw: next(replies))
        (record,) = realtime.native_citations
        assert record["url"] == FETCH_URL
        assert record["resolution"] == N.RESOLUTION_DOCUMENT_TEXT
        assert record["verdict_cites_source"] is True

        waves = {"verify__0": first}
        batch = run_batch(
            monkeypatch, lambda cid: waves.get(cid, second), max_waves=2
        ).verification
        (batch_record,) = batch.native_citations
        assert _without_transport_identity([batch_record]) == _without_transport_identity([record])

    def test_a_failure_keeps_the_citations_it_read(self, monkeypatch):
        blocks = [*search_blocks(), cited_text_block(search_citation())]
        result, _ = run_realtime(monkeypatch, message(blocks, stop_reason="max_tokens"))
        assert result.verification_failed is True
        (record,) = result.native_citations
        assert record["url"] == SEARCHED_URL
        assert record["verdict_cites_source"] is False

    def test_no_response_means_not_captured(self, monkeypatch):
        result, _ = run_realtime(monkeypatch, lambda _kw: RuntimeError("boom"))
        assert result.verification_failed is True
        assert result.native_citations is None
        assert N.capture_status(result) == N.STATUS_NOT_CAPTURED

    def test_a_conversation_without_citations_reads_none_returned(self, monkeypatch):
        result, _ = run_realtime(
            monkeypatch, message([*search_blocks(), verdict_call(verdict_payload())])
        )
        assert result.native_citations == []
        assert N.capture_status(result) == N.STATUS_NONE_RETURNED


class TestEscalationAndFallback:
    def _result(self, *, url: str, role: str, model: str, verdict: str, grounded: bool = True) -> VerificationResult:
        record = {
            **N.collect_native_citations([conversation(cited_text_block(search_citation(url=url)))])[0],
            "role": role, "model": model, "attempt_id": f"message:{role}",
        }
        return VerificationResult(
            verdict=verdict, grounded=grounded, sources=[url], accepted_sources=[url],
            model_used=model, native_citations=[record],
        )

    def test_both_passes_are_kept_and_labelled_kept_first(self):
        initial = self._result(url="https://a.example/1", role="primary", model="claude-sonnet-5", verdict="UNVERIFIED")
        escalated = self._result(url="https://b.example/2", role="escalation", model="claude-opus-5", verdict="CONFIRMED")
        merged = V._apply_escalation_outcome(
            initial_result=initial, esc_result=escalated, initial_verdict="UNVERIFIED",
            initial_model="claude-sonnet-5", initial_grounded=True,
            initial_sources=["https://a.example/1"], escalation_reason="initial_unverified",
        )
        assert merged is escalated
        assert [(r["role"], r["model"]) for r in merged.native_citations] == [
            ("escalation", "claude-opus-5"),
            ("primary", "claude-sonnet-5"),
        ]

    def test_fallback_relabel_renames_primary_and_retry_only(self):
        records = [{"role": "primary"}, {"role": "retry"}, {"role": "escalation"}]
        relabeled = N.relabel_roles(records, from_roles=("primary", "retry"), to_role="fallback")
        assert [r["role"] for r in relabeled] == ["fallback", "fallback", "escalation"]
        assert N.relabel_roles(None, from_roles=("primary",), to_role="fallback") is None


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _grounded(**overrides) -> VerificationResult:
    fields_ = dict(
        verdict="CONFIRMED", grounded=True, sources=[SEARCHED_URL],
        accepted_sources=[SEARCHED_URL], searched_sources=[SEARCHED_URL],
        source_quote=QUOTE, model_used="claude-sonnet-5",
    )
    fields_.update(overrides)
    return VerificationResult(**fields_)


def _save_and_reload(tmp_path: Path, result: VerificationResult) -> VerificationResult:
    finding = medium_finding()
    cache = VerificationCache()
    cache.put(finding, cycle=DEFAULT_CYCLE, result=result)
    path = tmp_path / "cache.json"
    cache.save_to_disk(path)
    reloaded = VerificationCache()
    reloaded.load_from_disk(path)
    hit = reloaded.get(finding, cycle=DEFAULT_CYCLE)
    assert hit is not None
    return hit


class TestCache:
    def test_citations_round_trip_and_replay_as_a_cache_replay(self, tmp_path, monkeypatch):
        fresh, _ = run_realtime(monkeypatch, cited_verdict_message())
        hit = _save_and_reload(tmp_path, fresh)
        assert hit.native_citations == fresh.native_citations
        assert N.capture_status(hit) == N.STATUS_CAPTURED
        assert N.provenance(hit) == N.PROVENANCE_CACHE_REPLAY

    def test_a_legacy_row_is_not_captured_not_none(self, tmp_path):
        finding = medium_finding()
        cache = VerificationCache()
        cache.put(finding, cycle=DEFAULT_CYCLE, result=_grounded())
        path = tmp_path / "cache.json"
        cache.save_to_disk(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        for entry in payload["entries"].values():
            entry["result"].pop("native_citations", None)
            entry["result"].pop("native_citations_omitted", None)
        path.write_text(json.dumps(payload), encoding="utf-8")
        reloaded = VerificationCache()
        assert reloaded.load_from_disk(path) == 1
        hit = reloaded.get(finding, cycle=DEFAULT_CYCLE)
        assert hit.native_citations is None
        assert N.capture_status(hit) == N.STATUS_NOT_CAPTURED
        concepts = RE._evidence_concepts(hit)
        assert concepts["attribution"] == (
            "not recorded: this verdict was cached before native citations were captured."
        )

    def test_an_empty_list_stays_none_returned(self, tmp_path):
        hit = _save_and_reload(tmp_path, _grounded(native_citations=[]))
        assert hit.native_citations == []
        assert N.capture_status(hit) == N.STATUS_NONE_RETURNED

    def test_no_fetched_document_is_persisted(self, tmp_path, monkeypatch):
        marker = "UNIQUE-DOCUMENT-BODY-MARKER"
        body = DOC_TEXT + " " + (marker + " ") * 5_000
        msg = message(
            [
                *search_blocks(),
                *fetch_pair(text=body),
                cited_text_block(char_citation()),
                verdict_call(verdict_payload()),
            ],
            fetches=1,
        )
        fresh, _ = run_realtime(monkeypatch, msg)
        finding = medium_finding()
        cache = VerificationCache()
        cache.put(finding, cycle=DEFAULT_CYCLE, result=fresh)
        path = tmp_path / "cache.json"
        cache.save_to_disk(path)
        saved = path.read_text(encoding="utf-8")
        assert marker not in saved
        assert DOC_SENTENCE in saved  # the bounded cited passage is kept

    def test_an_over_long_hand_edited_row_is_rebounded_and_counted(self, tmp_path):
        record = N.collect_native_citations([conversation(cited_text_block(search_citation()))])[0]
        many = [
            {**record, "url": f"https://x.example/{i}", "cited_text": "y" * 2_000}
            for i in range(N.MAX_NATIVE_CITATIONS + 3)
        ]
        hit = _save_and_reload(tmp_path, _grounded(native_citations=many, native_citations_omitted=1))
        assert len(hit.native_citations) == N.MAX_NATIVE_CITATIONS
        assert hit.native_citations_omitted == 4
        assert all(len(r["cited_text"]) == N.MAX_CITED_TEXT_CHARS for r in hit.native_citations)

    @pytest.mark.parametrize("bad", ["x", [7], {"url": "u"}], ids=["string", "non_dict_entry", "dict"])
    def test_a_malformed_citation_list_is_an_invalid_row(self, tmp_path, bad):
        finding = medium_finding()
        cache = VerificationCache()
        cache.put(finding, cycle=DEFAULT_CYCLE, result=_grounded())
        path = tmp_path / "cache.json"
        cache.save_to_disk(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        for entry in payload["entries"].values():
            entry["result"]["native_citations"] = bad
        path.write_text(json.dumps(payload), encoding="utf-8")
        reloaded = VerificationCache()
        assert reloaded.load_from_disk(path) == 0
        assert reloaded.stats()["rejected_on_load"] == 1

    def test_eligibility_does_not_read_citations(self):
        uncited = _grounded(sources=[], accepted_sources=[])
        record = N.collect_native_citations([conversation(cited_text_block(search_citation()))])[0]
        uncited_with_native = _grounded(sources=[], accepted_sources=[], native_citations=[record])
        assert cache_ineligibility_reason(uncited) == cache_ineligibility_reason(uncited_with_native)
        assert cache_ineligibility_reason(uncited_with_native) is not None

    def test_a_shared_clone_keeps_citations_and_reads_shared(self):
        from src.orchestration.pipeline import _shared_clone

        record = N.collect_native_citations([conversation(cited_text_block(search_citation()))])[0]
        clone = _shared_clone(_grounded(native_citations=[record], cache_status="miss"))
        assert clone.native_citations == [record]
        assert N.provenance(clone) == N.PROVENANCE_SHARED


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------


class _Recorder:
    is_deep = False

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def add_event(self, handle, kind, **fields):
        self.events.append((kind, fields))


class TestTrace:
    def test_the_verification_outputs_keep_the_three_concepts_apart(self, monkeypatch):
        fresh, _ = run_realtime(monkeypatch, cited_verdict_message())
        outputs = capture_hooks._verification_outputs(fresh, deep=False)
        assert outputs["native_citations"] == fresh.native_citations
        evidence = outputs["evidence"]
        assert set(evidence) == {"retrieval", "native_attribution", "semantic_support"}
        assert evidence["retrieval"]["searched_sources"] == 1
        assert evidence["retrieval"]["fetched_sources"] == 1
        assert evidence["native_attribution"]["status"] == N.STATUS_CAPTURED
        assert evidence["native_attribution"]["count"] == 2
        assert evidence["native_attribution"]["provenance"] == N.PROVENANCE_FRESH
        assert evidence["semantic_support"] == {"status": "not_assessed"}

    def test_a_legacy_result_traces_as_not_captured(self):
        outputs = capture_hooks._verification_outputs(_grounded(), deep=False)
        assert outputs["native_citations"] is None
        assert outputs["evidence"]["native_attribution"]["status"] == N.STATUS_NOT_CAPTURED

    def test_text_block_citations_become_an_event_with_unknowns_counted(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr(capture_hooks, "_get", lambda: recorder)
        unknown = {"type": "future_location", "cited_text": "z"}
        capture_hooks.capture_response_content_blocks(
            None, conversation({"type": "text", "text": "t", "citations": [search_citation(), unknown]})
        )
        (kind, fields_), = [e for e in recorder.events if e[0] == "native_citations"]
        assert fields_["count"] == 2
        assert fields_["unrecognized"] == 1
        assert fields_["citations"][0]["url"] == SEARCHED_URL


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def _docx_panel_text(vr: VerificationResult) -> str:
    doc = Document()
    RE._write_evidence_panel(doc, medium_finding(), vr)
    return "\n".join(p.text for p in doc.paragraphs)


def _html_panel(vr: VerificationResult) -> tuple[str, list[str]]:
    return HE._render_evidence_panel(medium_finding(), vr)


def _visible(html: str) -> str:
    """The HTML panel's text as a reader sees it (tags dropped, entities read)."""
    import html as _html
    import re

    return _html.unescape(re.sub(r"<[^>]+>", "", html))


class TestReports:
    def test_both_reports_state_the_three_concepts(self, monkeypatch):
        fresh, _ = run_realtime(monkeypatch, cited_verdict_message())
        word = _docx_panel_text(fresh)
        html, text_lines = _html_panel(fresh)
        for surface in (word, _visible(html), "\n".join(text_lines)):
            assert RE.EVIDENCE_CONCEPTS_HEADING in surface
            assert "Retrieval: 1 page(s) returned by web search and 1 page(s) read in full by web fetch in this verification." in surface
            assert "Native attribution: the API tied 2 passage(s)" in surface
            assert "not that the source supports the claim" in surface
            assert "Semantic support: not checked by this app." in surface
        concepts = RE._evidence_concepts(fresh)
        for line in concepts["citations"]:
            assert line in word
            assert line in "\n".join(text_lines)
        search_line, fetch_line = concepts["citations"]
        assert SEARCHED_URL in search_line and "the verdict cites this source" in search_line
        assert "web search" in search_line and "initial pass" in search_line
        assert FETCH_URL in fetch_line and "web fetch" in fetch_line and "characters 32–" in fetch_line

    def test_an_unresolved_citation_names_no_url(self):
        (record,) = N.collect_native_citations(
            [conversation(*fetch_pair(), cited_text_block(char_citation(index=4)))]
        )
        line = RE._native_citation_text(record)
        assert "source not established" in line
        assert FETCH_URL not in line
        assert "unresolved:" in line

    def test_a_replay_says_when_the_evidence_was_gathered(self):
        concepts = RE._evidence_concepts(_grounded(cache_status="hit", native_citations=[]))
        assert concepts["retrieval"].endswith("when this verdict was first reached (cache replay).")
        assert concepts["attribution"] == "the API attached no citations to the verifier's text."

    def test_a_local_classification_gets_no_evidence_block(self):
        local = V._local_skip_result()
        assert RE._evidence_concepts(local) is None
        assert RE.EVIDENCE_CONCEPTS_HEADING not in _docx_panel_text(local)
        html, _ = _html_panel(local)
        assert RE.EVIDENCE_CONCEPTS_HEADING not in html

    def test_hostile_cited_text_is_escaped_in_html(self):
        record = N.collect_native_citations(
            [conversation(cited_text_block(search_citation(text='<script>alert(1)</script>')))]
        )[0]
        html, _ = _html_panel(_grounded(native_citations=[record]))
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_the_html_payload_carries_the_records_and_their_status(self, monkeypatch):
        fresh, _ = run_realtime(monkeypatch, cited_verdict_message())
        payload = HE._serialize_verification(fresh)
        assert payload["native_citations"] == fresh.native_citations
        assert payload["native_citation_status"] == N.STATUS_CAPTURED
        legacy = HE._serialize_verification(_grounded())
        assert legacy["native_citations"] is None
        assert legacy["native_citation_status"] == N.STATUS_NOT_CAPTURED


# ---------------------------------------------------------------------------
# Fetch instructions
# ---------------------------------------------------------------------------


def _fetch_block(prompt: str) -> str:
    start = prompt.index("<web_fetch_usage>")
    return prompt[start: prompt.index("</web_fetch_usage>", start)]


class TestFetchInstructions:
    @pytest.mark.parametrize("module", list(AVAILABLE_MODULES.values()), ids=lambda m: m.module_id)
    @pytest.mark.parametrize("with_tool", [True, False], ids=["verdict_tool", "no_verdict_tool"])
    def test_any_url_already_in_the_conversation_is_fetchable(self, module, with_tool):
        prompt = V._get_verification_system_prompt(module.cycle, include_verdict_tool=with_tool)
        block = " ".join(_fetch_block(prompt).split())
        # The provider's rule, both allowed sources named.
        assert "only a URL that already appears in this conversation" in block
        assert "one written in the finding you were given" in block
        assert "earlier web_search or web_fetch result returned" in block
        # And what it cannot open.
        assert "appears only in these instructions or only in your own writing" in block
        # The superseded search-only rule is gone.
        assert "ONLY retrieve" not in prompt
        assert "previously appeared in a web_search result" not in prompt

    @pytest.mark.parametrize("module", list(AVAILABLE_MODULES.values()), ids=lambda m: m.module_id)
    def test_a_supplied_url_still_gets_every_check(self, module):
        block = " ".join(_fetch_block(V._get_verification_system_prompt(module.cycle)).split())
        assert "A URL the finding supplies is a lead, not evidence." in block
        for check in (
            "supports (or contradicts) the claim",
            "the edition that governs this project",
            "authority over the requirement",
            "applies to this project's scope",
        ):
            assert check in block, check
        assert "Cite it only when it does." in block

    def test_the_block_still_applies_only_when_the_tool_is_attached(self):
        block = _fetch_block(V._get_verification_system_prompt(DEFAULT_CYCLE))
        assert block.splitlines()[1] == "Applies when web_fetch is attached to this call."

    def test_capability_gates_are_unchanged(self):
        from src.verification.verification_modes import VerificationMode
        from src.verification.verification_routing import (
            build_verification_tools_from_decision,
            select_routing,
        )

        finding = medium_finding()
        decision = select_routing(finding, escalated=False, local_skip=False, cycle=DEFAULT_CYCLE)
        names = lambda d: [t.get("name") for t in build_verification_tools_from_decision(d)]  # noqa: E731
        assert decision.mode == VerificationMode.STANDARD_REASONING.value
        assert "web_fetch" in names(decision)  # Sonnet 5 supports fetch
        escalated = select_routing(finding, escalated=True, local_skip=False, cycle=DEFAULT_CYCLE)
        assert "web_fetch" not in names(escalated)  # Opus 5 does not
        strict = select_routing(
            medium_finding(severity="GRIPES", codeReference=None),
            escalated=False, local_skip=False, cycle=DEFAULT_CYCLE,
        )
        assert "web_fetch" not in names(strict)
