"""The API's own citations for the verifier's sources (plan WP-16).

When the verifier writes text about what it retrieved, the Messages API
attaches **native citations** to that text: a ``web_search_result_location``
names the search result a sentence came from (URL, title, and up to 150
characters of cited text), and a ``char_location`` / ``page_location`` /
``content_block_location`` points into a fetched document by its
*document index* plus a character, page, or block range. Until this module,
every one of them was discarded. This module keeps them, bounded, beside the
verdict they came with.

Three things must never be confused, and this module is where the second one
is defined:

* **Retrieval** — a page the verification tools actually returned in this
  conversation (``VerificationResult.searched_sources`` /
  ``fetched_sources``). The grounding gate rests on it
  (``source_grounding.validate_cited_sources``).
* **Native attribution** — a citation the API attached to the verifier's own
  words: *which retrieved passage the text came from*. The API extracts
  ``cited_text`` from the source itself, so a native citation cannot quote
  something the source does not contain. It still says nothing about whether
  the passage supports the finding's claim or its proposed edit.
* **Semantic support** — whether a source actually substantiates the claim.
  Nothing in this app establishes it (a stronger check is plan EX-04, run in
  observation mode first). Reports say so rather than let a citation stand in
  for it.

Native citations never change a verdict, its grounding, its cache
eligibility, or its report status: they are recorded, displayed, and cached
as evidence of attribution only. In particular no text-overlap measure
decides anything here — the one text comparison below (the cited text found
in the document a citation's index names) decides only *which document* a
citation points at, never whether a verdict is accepted.

**Document-index citations resolve only through their own conversation's
documents.** The provider counts a citation's ``document_index`` across the
document blocks of the conversation that produced it. The verifier's request
carries no documents of its own, so the documents are the ``web_fetch``
results, in conversation order, and a citation can only point at one that
appeared *before* it (the model cannot cite a page it has not read yet). An
index is resolved to that document's URL only when nothing contradicts the
mapping: an out-of-range index, a different document title, or cited text the
document does not contain leaves the citation **unresolved**, with the reason
recorded — never attached to a nearby URL. When the document's text cannot be
read (a fetched PDF arrives as base64), a resolution by position alone is
labelled as such.

**Bounded.** A result keeps at most :data:`MAX_NATIVE_CITATIONS` records, each
cited text at most :data:`MAX_CITED_TEXT_CHARS` characters. Whole fetched
documents are never kept: the document text read during resolution is
dropped when collection returns. Records the cap drops are counted
(``native_citations_omitted``), never silently lost.

**Unknown shapes stay visible.** A citation of a type this build does not
recognize becomes a record with ``recognized: False``, its type, its field
names, and its cited text — it never discards the otherwise valid result it
arrived with.

Stdlib plus :mod:`source_grounding` only, so the verifier, the cache, the
tracer, and both report exporters can share it without an import cycle.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .source_grounding import normalize_url

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: The citation types this build reads. Anything else is recorded as an
#: unrecognized shape.
CITATION_WEB_SEARCH = "web_search_result_location"
CITATION_CHAR = "char_location"
CITATION_PAGE = "page_location"
CITATION_CONTENT_BLOCK = "content_block_location"
CITATION_SEARCH_RESULT = "search_result_location"

DOCUMENT_CITATION_TYPES = frozenset({CITATION_CHAR, CITATION_PAGE, CITATION_CONTENT_BLOCK})
RECOGNIZED_CITATION_TYPES = frozenset(
    {CITATION_WEB_SEARCH, CITATION_SEARCH_RESULT, *DOCUMENT_CITATION_TYPES}
)

#: The retrieval tool a citation's source came from.
TOOL_WEB_SEARCH = "web_search"
TOOL_WEB_FETCH = "web_fetch"
#: A client-supplied search result block. The verifier supplies one only
#: under the source-reuse experiment (plan EX-04, off by default): a passage
#: another finding's verification retrieved earlier in the run. Such a record
#: is never "retrieved" by the conversation that cites it.
TOOL_SEARCH_RESULT = "search_result"

#: How a citation's source was identified.
#: The citation names its source itself (a search result's URL).
RESOLUTION_DIRECT = "direct"
#: A document index resolved to a fetched document whose text contains the
#: cited text.
RESOLUTION_DOCUMENT_TEXT = "document_text"
#: A document index resolved by position alone: the document's text could not
#: be read to confirm the cited text (a fetched PDF), and nothing contradicted
#: the mapping.
RESOLUTION_DOCUMENT_INDEX = "document_index"
#: The source could not be established; ``url`` is empty and
#: ``resolution_note`` says why.
RESOLUTION_UNRESOLVED = "unresolved"
RESOLUTIONS = frozenset(
    {
        RESOLUTION_DIRECT,
        RESOLUTION_DOCUMENT_TEXT,
        RESOLUTION_DOCUMENT_INDEX,
        RESOLUTION_UNRESOLVED,
    }
)

#: Whether a result carries native citations at all — see :func:`capture_status`.
STATUS_NOT_CAPTURED = "not_captured"
STATUS_NONE_RETURNED = "none_returned"
STATUS_CAPTURED = "captured"

#: Where a result's native citations came from — see :func:`provenance`.
PROVENANCE_FRESH = "fresh"
PROVENANCE_CACHE_REPLAY = "cache_replay"
PROVENANCE_SHARED = "shared"
PROVENANCE_NONE = "none"

# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

#: At most this many records per verification result (both passes of an
#: escalation together). A verification conversation writes a handful of
#: cited sentences; the bound keeps the cache and the report proportionate.
MAX_NATIVE_CITATIONS = 20
#: A web search citation's cited text is at most 150 characters by API
#: contract; a document citation's is a sentence or a page chunk. Longer text
#: is cut here and flagged ``cited_text_truncated``.
MAX_CITED_TEXT_CHARS = 500
MAX_TITLE_CHARS = 300
MAX_URL_CHARS = 2048
#: Field names kept for an unrecognized citation shape.
MAX_UNKNOWN_FIELDS = 16
_MAX_TEXT_FIELD_CHARS = 200

# The locator fields each recognized document / search-result type carries.
# A web search citation's only locator is ``encrypted_index``: an opaque
# token for replaying the citation to the API, which identifies nothing a
# reader can use, so it is not kept.
_LOCATOR_FIELDS: dict[str, tuple[str, ...]] = {
    CITATION_CHAR: ("start_char_index", "end_char_index"),
    CITATION_PAGE: ("start_page_number", "end_page_number"),
    CITATION_CONTENT_BLOCK: ("start_block_index", "end_block_index"),
    CITATION_SEARCH_RESULT: ("search_result_index", "start_block_index", "end_block_index"),
}

_WHITESPACE = re.compile(r"\s+")


def _get(node: Any, name: str) -> Any:
    """Read ``name`` from an SDK object or a plain dict (batch results are dicts)."""
    if isinstance(node, dict):
        return node.get(name)
    return getattr(node, name, None)


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _bounded(value: Any, limit: int) -> tuple[str, bool]:
    """A string cut to ``limit`` characters, and whether it was cut."""
    if not isinstance(value, str):
        return "", False
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _count(value: Any) -> int | None:
    """A non-negative whole number, or ``None`` (a bool is not a count)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _field_names(node: Any) -> list[str]:
    if isinstance(node, dict):
        names = [str(k) for k in node]
    else:
        dumper = getattr(node, "model_dump", None)
        names = []
        if callable(dumper):
            try:
                dumped = dumper()
                if isinstance(dumped, dict):
                    names = [str(k) for k in dumped]
            except Exception:
                names = []
        if not names and hasattr(node, "__dict__"):
            names = [str(k) for k in vars(node) if not str(k).startswith("_")]
    return sorted(names)[:MAX_UNKNOWN_FIELDS]


# ---------------------------------------------------------------------------
# Documents in a conversation
# ---------------------------------------------------------------------------


class _Document:
    """One document block the conversation holds, in order.

    ``text`` is the document's plain text when its source is text, else
    ``None`` (a base64 PDF): it exists only to confirm a citation's mapping
    and never leaves this module.
    """

    __slots__ = ("url", "title", "text", "_collapsed")

    def __init__(self, url: str, title: str, text: str | None) -> None:
        self.url = url
        self.title = title
        self.text = text
        self._collapsed: str | None = None

    def contains(self, cited_text: str) -> bool:
        """Whether the document's text holds ``cited_text`` (whitespace-collapsed).

        The collapsed page is computed once per document, not once per
        citation: a fetched page can run to tens of thousands of characters.
        """
        if self._collapsed is None:
            self._collapsed = _collapse(self.text or "")
        return _collapse(cited_text) in self._collapsed


def _fetched_document(block: Any, fetch_urls: dict[str, str]) -> _Document | None:
    """The document a ``web_fetch_tool_result`` block holds, or ``None``.

    An error result holds no document, so it adds nothing to the document
    count. Reads the current shape (``content.content``, text in
    ``source.data``) and the older echo (``content.document``, text in
    ``document.content``), as the fetch-evidence collector does. When the
    result carries no URL, the URL the paired ``web_fetch`` call asked for
    (``fetch_urls``, keyed by the call's id) is used — found by
    ``tool_use_id`` only, never by position.
    """
    result = _get(block, "content")
    if result is None:
        return None
    if _get(result, "type") == "web_fetch_tool_result_error":
        return None
    document = _get(result, "content")
    if document is None or isinstance(document, str):
        document = _get(result, "document")
    if document is None or isinstance(document, str):
        return None
    url = _get(result, "url") or _get(document, "url") or ""
    if not isinstance(url, str) or not url.strip():
        tool_use_id = _get(block, "tool_use_id")
        url = fetch_urls.get(tool_use_id, "") if isinstance(tool_use_id, str) else ""
    title = _get(document, "title") or ""
    source = _get(document, "source")
    text: str | None = None
    if source is not None and _get(source, "type") == "text":
        data = _get(source, "data")
        if isinstance(data, str):
            text = data
    if text is None:
        # The older echo carries the page body as a string on the document.
        body = _get(document, "content")
        if isinstance(body, str):
            text = body
    return _Document(
        url=url.strip(),
        title=str(title) if isinstance(title, str) else "",
        text=text,
    )


def _resolve_document(
    index: Any, *, title: str, cited_text: str, documents: list[_Document]
) -> tuple[_Document | None, str, str]:
    """Resolve a document index against the documents seen so far.

    Returns ``(document, resolution, note)``; ``document`` is ``None`` exactly
    when the resolution is :data:`RESOLUTION_UNRESOLVED`.
    """
    position = _count(index)
    if position is None:
        return None, RESOLUTION_UNRESOLVED, "the citation carries no valid document index"
    if position >= len(documents):
        return (
            None,
            RESOLUTION_UNRESOLVED,
            f"document index {position} names no document retrieved before it "
            "in this conversation",
        )
    document = documents[position]
    if title and document.title and _collapse(title) != _collapse(document.title):
        return (
            None,
            RESOLUTION_UNRESOLVED,
            f"the document at index {position} has a different title",
        )
    if not document.url:
        return (
            None,
            RESOLUTION_UNRESOLVED,
            f"the document at index {position} has no URL",
        )
    if document.text is not None and cited_text.strip():
        if document.contains(cited_text):
            return document, RESOLUTION_DOCUMENT_TEXT, ""
        return (
            None,
            RESOLUTION_UNRESOLVED,
            f"the cited text is not in the document at index {position}",
        )
    return (
        document,
        RESOLUTION_DOCUMENT_INDEX,
        "matched by position only: the document's text could not be read to "
        "confirm the cited text",
    )


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _base_record(citation_type: str, *, recognized: bool, cited_text: Any) -> dict:
    text, truncated = _bounded(cited_text, MAX_CITED_TEXT_CHARS)
    return {
        "type": citation_type,
        "recognized": recognized,
        "tool": "",
        "url": "",
        "title": "",
        "cited_text": text,
        "cited_text_truncated": truncated,
        "document_index": None,
        "locator": {},
        "resolution": RESOLUTION_UNRESOLVED,
        "resolution_note": "",
        "retrieved": False,
        "verdict_cites_source": False,
        "model": "",
        "role": "",
        "transport": "",
        "attempt_id": "",
    }


def _set_url(record: dict, url: Any) -> None:
    text, _ = _bounded(url if isinstance(url, str) else "", MAX_URL_CHARS)
    record["url"] = text.strip()


def _set_title(record: dict, title: Any) -> None:
    text, _ = _bounded(title if isinstance(title, str) else "", MAX_TITLE_CHARS)
    record["title"] = text.strip()


def _locator(citation: Any, citation_type: str) -> dict:
    locator: dict[str, int] = {}
    for name in _LOCATOR_FIELDS.get(citation_type, ()):
        value = _count(_get(citation, name))
        if value is not None:
            locator[name] = value
    return locator


def _citation_record(citation: Any, documents: list[_Document]) -> dict:
    """One record for one native citation, resolved against ``documents``."""
    citation_type = _get(citation, "type")
    if not isinstance(citation_type, str) or citation_type not in RECOGNIZED_CITATION_TYPES:
        record = _base_record(
            citation_type if isinstance(citation_type, str) else "",
            recognized=False,
            cited_text=_get(citation, "cited_text"),
        )
        record["fields"] = _field_names(citation)
        record["resolution_note"] = "citation shape not recognized by this build"
        return record

    cited_text = _get(citation, "cited_text")
    record = _base_record(citation_type, recognized=True, cited_text=cited_text)
    record["locator"] = _locator(citation, citation_type)

    if citation_type == CITATION_WEB_SEARCH:
        record["tool"] = TOOL_WEB_SEARCH
        _set_url(record, _get(citation, "url"))
        _set_title(record, _get(citation, "title"))
        if record["url"]:
            record["resolution"] = RESOLUTION_DIRECT
        else:
            record["resolution_note"] = "the citation carries no URL"
        return record

    if citation_type == CITATION_SEARCH_RESULT:
        record["tool"] = TOOL_SEARCH_RESULT
        _set_url(record, _get(citation, "source"))
        _set_title(record, _get(citation, "title"))
        if record["url"]:
            record["resolution"] = RESOLUTION_DIRECT
        else:
            record["resolution_note"] = "the citation names no source"
        return record

    # A document citation: resolve its index through this conversation's
    # own fetched documents, never through a nearby URL.
    index = _get(citation, "document_index")
    record["document_index"] = _count(index)
    document_title = _get(citation, "document_title")
    document_title = document_title if isinstance(document_title, str) else ""
    _set_title(record, document_title)
    document, resolution, note = _resolve_document(
        index,
        title=document_title,
        cited_text=cited_text if isinstance(cited_text, str) else "",
        documents=documents,
    )
    record["resolution"] = resolution
    record["resolution_note"] = note
    if document is not None:
        record["tool"] = TOOL_WEB_FETCH
        _set_url(record, document.url)
        if not record["title"]:
            _set_title(record, document.title)
    return record


def collect_native_citations(
    messages: Iterable[Any], *, attempt_metadata: Iterable[dict] | None = None,
) -> list[dict]:
    """Every native citation in a verification conversation, in order.

    ``messages`` are the conversation's assistant turns in order: the
    real-time loop's responses, or the batch path's whole-conversation view
    (one message whose content is every wave's blocks). Each text block's
    ``citations`` become records; a document citation resolves against the
    documents that appeared before its text block. A ``citations`` value that
    is present but not a list is recorded as one unrecognized shape.
    """
    # Optional per-response attribution keeps document resolution across the
    # full history while naming the individual request that wrote the text.
    metadata = iter(attempt_metadata) if attempt_metadata is not None else None
    records: list[dict] = []
    documents: list[_Document] = []
    # The URL each ``web_fetch`` call asked for, by call id: the fallback for a
    # result block that does not echo its URL.
    fetch_urls: dict[str, str] = {}
    for message in messages or []:
        first_record = len(records)
        for block in _get(message, "content") or []:
            block_type = _get(block, "type")
            if block_type == "server_tool_use" and _get(block, "name") == "web_fetch":
                call_id = _get(block, "id")
                tool_input = _get(block, "input")
                url = tool_input.get("url") if isinstance(tool_input, dict) else None
                if isinstance(call_id, str) and isinstance(url, str) and url.strip():
                    fetch_urls[call_id] = url.strip()
                continue
            if block_type == "web_fetch_tool_result":
                document = _fetched_document(block, fetch_urls)
                if document is not None:
                    documents.append(document)
                continue
            if block_type != "text":
                continue
            citations = _get(block, "citations")
            if citations is None:
                continue
            if not isinstance(citations, (list, tuple)):
                record = _base_record("", recognized=False, cited_text=None)
                record["fields"] = []
                record["resolution_note"] = "the text block's citations field is not a list"
                records.append(record)
                continue
            for citation in citations:
                records.append(_citation_record(citation, documents))
        if metadata is not None:
            attempt = next(metadata)
            records[first_record:] = attribute_native_citations(
                records[first_record:],
                attempt_id=str(attempt.get("attempt_id", "")),
                role=str(attempt.get("role", "")),
                model=str(attempt.get("model", "")),
                transport=str(attempt.get("transport", "")),
            )
    return records


# ---------------------------------------------------------------------------
# Association, attribution, and bounds
# ---------------------------------------------------------------------------


def _dedupe_key(record: dict) -> tuple:
    return (
        record.get("type", ""),
        normalize_url(record.get("url", "")),
        record.get("document_index"),
        record.get("cited_text", ""),
        tuple(sorted((record.get("locator") or {}).items())),
        record.get("attempt_id", ""),
        record.get("role", ""),
    )


def cap_native_citations(
    records: list[dict], *, omitted: int = 0
) -> tuple[list[dict], int]:
    """Deduplicate and bound ``records``; return ``(kept, omitted)``.

    Duplicates (the same citation written twice) collapse to one. Past
    :data:`MAX_NATIVE_CITATIONS`, records whose source the verdict itself
    cites are kept before any other, then the rest in order; the kept
    records stay in their existing order, and what the cap drops is added
    to ``omitted``.
    """
    seen: set[tuple] = set()
    unique: list[dict] = []
    for record in records:
        key = _dedupe_key(record)
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    if len(unique) <= MAX_NATIVE_CITATIONS:
        return unique, omitted
    ranked = sorted(
        range(len(unique)),
        key=lambda i: (not unique[i].get("verdict_cites_source", False), i),
    )
    keep = set(ranked[:MAX_NATIVE_CITATIONS])
    kept = [record for i, record in enumerate(unique) if i in keep]
    return kept, omitted + (len(unique) - len(kept))


def associate_native_citations(
    records: list[dict] | None,
    *,
    retrieved_urls: Iterable[str],
    verdict_sources: Iterable[str],
    model: str = "",
    transport: str = "",
) -> tuple[list[dict] | None, int]:
    """Tie a conversation's citations to its retrieval and its verdict.

    ``retrieved`` says the citation's source is among the pages the tools
    returned in this conversation; ``verdict_cites_source`` says the verdict
    this conversation produced cites the same source (it never says the
    source supports that verdict). ``None`` stays ``None``: a conversation
    whose blocks were never read has no citations to tie.
    """
    if records is None:
        return None, 0
    retrieved = {normalize_url(u) for u in retrieved_urls or [] if normalize_url(u)}
    cited = {normalize_url(u) for u in verdict_sources or [] if normalize_url(u)}
    tied: list[dict] = []
    for record in records:
        record = dict(record)
        key = normalize_url(record.get("url", ""))
        record["retrieved"] = bool(key) and key in retrieved
        record["verdict_cites_source"] = bool(key) and key in cited
        if model:
            record["model"] = model
        if transport:
            record["transport"] = transport
        tied.append(record)
    return cap_native_citations(tied)


def attribute_native_citations(
    records: list[dict] | None,
    *,
    attempt_id: str = "",
    role: str = "",
    model: str = "",
    transport: str = "",
) -> list[dict] | None:
    """Stamp the attempt that produced ``records`` on each one lacking it."""
    if records is None:
        return None
    out: list[dict] = []
    for record in records:
        record = dict(record)
        if attempt_id and not record.get("attempt_id"):
            record["attempt_id"] = attempt_id
        if role and not record.get("role"):
            record["role"] = role
        if model and not record.get("model"):
            record["model"] = model
        if transport and not record.get("transport"):
            record["transport"] = transport
        out.append(record)
    return out


def relabel_roles(
    records: list[dict] | None, *, from_roles: Iterable[str], to_role: str
) -> list[dict] | None:
    """Rename the attempt role on records whose role is in ``from_roles``."""
    if records is None:
        return None
    wanted = set(from_roles)
    return [
        {**record, "role": to_role} if record.get("role") in wanted else dict(record)
        for record in records
    ]


def combine_native_citations(
    kept: list[dict] | None,
    kept_omitted: int,
    other: list[dict] | None,
    other_omitted: int,
) -> tuple[list[dict] | None, int]:
    """One result's citations from two conversations (an escalation).

    The kept verdict's conversation comes first, so the cap favors it within
    each tier. ``None`` only when neither conversation's blocks were read.
    """
    if kept is None and other is None:
        return None, 0
    merged = list(kept or []) + list(other or [])
    return cap_native_citations(merged, omitted=int(kept_omitted or 0) + int(other_omitted or 0))


# ---------------------------------------------------------------------------
# Reading a result
# ---------------------------------------------------------------------------


def capture_status(result: Any) -> str:
    """Whether ``result`` carries native citations, honestly labelled.

    * ``not_captured`` — nothing was read: a result built before native
      citations were captured (a legacy cache entry), a local
      classification, or a result whose conversation blocks were never read.
    * ``none_returned`` — the conversation was read and carried no citations.
    * ``captured`` — at least one record (or one the cap dropped).
    """
    records = getattr(result, "native_citations", None)
    if records is None:
        return STATUS_NOT_CAPTURED
    omitted = int(getattr(result, "native_citations_omitted", 0) or 0)
    if not records and omitted <= 0:
        return STATUS_NONE_RETURNED
    return STATUS_CAPTURED


def provenance(result: Any) -> str:
    """Where a result's evidence came from: this run, a cache replay, or a
    verdict shared from an equivalent finding in this run."""
    status = (getattr(result, "cache_status", "") or "").strip().lower()
    if status == "hit":
        return PROVENANCE_CACHE_REPLAY
    if status == "shared":
        return PROVENANCE_SHARED
    if status == "local_skip":
        return PROVENANCE_NONE
    return PROVENANCE_FRESH


def coerce_native_citations(raw: Any) -> tuple[list[dict] | None, int]:
    """Records from a persisted row, bounded and typed, and how many the cap
    dropped; ``(None, 0)`` when absent.

    Load-side defense for a cache file written by another build or edited by
    hand: every field is re-typed and re-bounded, non-dict entries are
    dropped, and the list is capped again (what that cap drops is counted,
    like any other). Never raises.
    """
    if raw is None or not isinstance(raw, list):
        return None, 0
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        citation_type = entry.get("type")
        record = _base_record(
            citation_type if isinstance(citation_type, str) else "",
            recognized=entry.get("recognized") is True,
            cited_text=entry.get("cited_text"),
        )
        if entry.get("cited_text_truncated") is True:
            record["cited_text_truncated"] = True
        for name in ("tool", "model", "role", "transport", "attempt_id"):
            value, _ = _bounded(entry.get(name), _MAX_TEXT_FIELD_CHARS)
            record[name] = value
        _set_url(record, entry.get("url"))
        _set_title(record, entry.get("title"))
        record["document_index"] = _count(entry.get("document_index"))
        locator = entry.get("locator")
        if isinstance(locator, dict):
            record["locator"] = {
                str(k): v for k, v in locator.items() if _count(v) is not None
            }
        resolution = entry.get("resolution")
        record["resolution"] = (
            resolution if resolution in RESOLUTIONS else RESOLUTION_UNRESOLVED
        )
        if record["resolution"] != RESOLUTION_UNRESOLVED and not record["url"]:
            record["resolution"] = RESOLUTION_UNRESOLVED
        note, _ = _bounded(entry.get("resolution_note"), _MAX_TEXT_FIELD_CHARS)
        record["resolution_note"] = note
        record["retrieved"] = entry.get("retrieved") is True
        record["verdict_cites_source"] = entry.get("verdict_cites_source") is True
        fields = entry.get("fields")
        if not record["recognized"]:
            record["fields"] = [
                str(f) for f in (fields if isinstance(fields, list) else [])
                if isinstance(f, str)
            ][:MAX_UNKNOWN_FIELDS]
        out.append(record)
    return cap_native_citations(out)


def unrecognized_count(records: Iterable[dict] | None) -> int:
    """How many records are shapes this build did not recognize."""
    return sum(1 for r in records or [] if not r.get("recognized", False))
