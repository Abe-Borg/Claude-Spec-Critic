"""Source-grounding helpers for verification (Chunk H).

The verifier previously had two parallel notions of "sources":

- ``_collect_search_evidence`` returned the flat list of every URL the
  web_search server tool retrieved across all of the model's queries.
- The structured ``submit_verification_verdict`` payload returned a
  ``sources`` list curated by the model.

There was no consistency check between the two: a model that cited a URL
the API never actually retrieved (a hallucinated source) would have been
accepted just like one that cited a real result. The verifier system
prompt asks the model not to invent URLs, but Chunk H Directive 2 calls
for programmatic enforcement.

This module owns the contract:

1. URLs flow through :func:`normalize_url` before being compared so that
   trivial differences (scheme, default port, trailing slash, tracking
   parameters, fragment, query-param ordering) do not falsely reject a
   real cited source.
2. :func:`validate_cited_sources` partitions the model's cited URLs into
   *accepted* (matched a real search result) and *rejected* (did not).
   Rejected entries carry a structured reason so reports and diagnostics
   can explain the downgrade.
3. :func:`is_grounded_against_search_results` is the single boolean used
   to gate ``CONFIRMED`` / ``CORRECTED`` verdicts when at least one
   cited source was supplied.

The helpers are deliberately string-only: no I/O, no network. They are
called from inside :mod:`src.verifier` immediately after a verdict is
parsed, while ``_collect_search_evidence`` is still in scope.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import parse_qsl, urlsplit, urlunsplit, unquote


_TRACKING_QUERY_KEYS = frozenset({
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "gclid",
    "fbclid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "ref",
    "ref_src",
    "ref_url",
    "_ga",
    "_gl",
})


_EQUIVALENT_SCHEMES = frozenset({"http", "https"})


def _strip_default_port(host: str, scheme: str) -> str:
    """Drop default ports (80 for http, 443 for https) from ``host``.

    ``scheme`` here is the **post-fold** value; once http/https are
    folded to https, port 80 is equally trivial to strip. We treat both
    default ports as equivalent so a search result on port 80 still
    matches a model citation without an explicit port.
    """
    if not host or ":" not in host:
        return host
    bare, _, port = host.rpartition(":")
    if not bare:
        return host
    if scheme == "https" and port in ("80", "443"):
        return bare
    if scheme == "http" and port == "80":
        return bare
    return host


def _strip_trailing_slash(path: str) -> str:
    """Drop a trailing slash. Root path ``/`` is collapsed to ``""`` so
    ``https://host/`` and ``https://host`` normalize equally.
    """
    if path == "/":
        return ""
    if len(path) > 1 and path.endswith("/"):
        return path[:-1]
    return path


def normalize_url(url: str | None) -> str:
    """Return a canonical form of ``url`` suitable for grounding comparison.

    Rules:

    - ``http`` and ``https`` are folded to ``https`` (the model and the
      search tool routinely disagree on scheme for the same source).
    - Host is lowercased; default ports are dropped; trailing dot on the
      host is dropped.
    - Path is left as-is except a single trailing slash is removed when
      it would otherwise be the only difference between two URLs.
    - Query parameters are URL-decoded, sorted by key, and well-known
      tracking parameters are dropped.
    - Fragment is dropped — fragments are client-side anchors and never
      change which page a citation refers to.

    Falsy / non-string input returns ``""`` so callers can use the
    result as a dict key without separate validation.
    """
    if not url or not isinstance(url, str):
        return ""
    cleaned = url.strip()
    if not cleaned:
        return ""
    if cleaned.startswith("<") and cleaned.endswith(">"):
        cleaned = cleaned[1:-1].strip()
    trailing_punct = set(",;)]}.'\"")
    while cleaned and cleaned[-1] in trailing_punct:
        cleaned = cleaned[:-1].rstrip()
    if not cleaned:
        return ""
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return ""
    scheme = (parts.scheme or "").lower()
    if not scheme and not parts.netloc and parts.path:
        return normalize_url("https://" + cleaned)
    if scheme in _EQUIVALENT_SCHEMES:
        scheme = "https"
    host = (parts.netloc or "").lower()
    if "@" in host:
        host = host.split("@", 1)[1]
    host = host.rstrip(".")
    host = _strip_default_port(host, scheme)
    path = _strip_trailing_slash(parts.path or "")
    raw_query = parts.query or ""
    if raw_query:
        items = parse_qsl(raw_query, keep_blank_values=True)
        kept = [(k, v) for k, v in items if k.lower() not in _TRACKING_QUERY_KEYS]
        kept.sort()
        from urllib.parse import urlencode
        query = urlencode(kept)
    else:
        query = ""
    return urlunsplit((scheme, host, path, query, ""))


REJECT_UNGROUNDED = "ungrounded"
REJECT_MALFORMED = "malformed"
REJECT_EMPTY = "empty"


@dataclass(frozen=True)
class CitedSourceVerdict:
    """Outcome of validating a single model-cited URL against search results."""

    url: str
    normalized: str
    accepted: bool
    reason: str = ""


@dataclass(frozen=True)
class SourceGroundingOutcome:
    """Aggregate outcome for the full cited-source set."""

    accepted: tuple[str, ...] = ()
    rejected: tuple[dict, ...] = ()
    verdicts: tuple[CitedSourceVerdict, ...] = ()

    def has_any_grounded_citation(self) -> bool:
        """Whether at least one cited URL matched an actual search result."""
        return len(self.accepted) > 0


def validate_cited_sources(
    cited: Iterable[str] | None,
    searched: Iterable[str] | None,
) -> SourceGroundingOutcome:
    """Validate model-cited URLs against the URLs the API actually fetched.

    Each cited URL is normalized via :func:`normalize_url` and looked up
    in the normalized set of search results. A miss becomes a rejection
    with ``reason=REJECT_UNGROUNDED``; an empty or unparseable URL
    becomes ``REJECT_EMPTY`` / ``REJECT_MALFORMED``. Accepted URLs are
    returned in their original (model-supplied) form so reports can
    render the model's exact citation text — the normalization is an
    internal comparison detail.

    If ``searched`` is empty, every cited URL is rejected as
    ``REJECT_UNGROUNDED``: without any retrieved evidence there is
    nothing to validate against.
    """
    cited_list = list(cited or [])
    searched_set = {normalize_url(u) for u in (searched or []) if normalize_url(u)}

    accepted_original: list[str] = []
    rejected_records: list[dict] = []
    verdicts: list[CitedSourceVerdict] = []
    seen_normalized: set[str] = set()

    for raw in cited_list:
        if not isinstance(raw, str) or not raw.strip():
            verdicts.append(
                CitedSourceVerdict(
                    url=str(raw or ""), normalized="", accepted=False, reason=REJECT_EMPTY
                )
            )
            rejected_records.append({"url": str(raw or ""), "reason": REJECT_EMPTY})
            continue
        normalized = normalize_url(raw)
        if not normalized:
            verdicts.append(
                CitedSourceVerdict(
                    url=raw, normalized="", accepted=False, reason=REJECT_MALFORMED
                )
            )
            rejected_records.append({"url": raw, "reason": REJECT_MALFORMED})
            continue
        if normalized in searched_set:
            if normalized in seen_normalized:
                continue
            seen_normalized.add(normalized)
            accepted_original.append(raw)
            verdicts.append(
                CitedSourceVerdict(
                    url=raw, normalized=normalized, accepted=True, reason=""
                )
            )
        else:
            verdicts.append(
                CitedSourceVerdict(
                    url=raw,
                    normalized=normalized,
                    accepted=False,
                    reason=REJECT_UNGROUNDED,
                )
            )
            rejected_records.append({"url": raw, "reason": REJECT_UNGROUNDED})

    return SourceGroundingOutcome(
        accepted=tuple(accepted_original),
        rejected=tuple(rejected_records),
        verdicts=tuple(verdicts),
    )


def is_grounded_against_search_results(
    cited: Iterable[str] | None,
    searched: Iterable[str] | None,
) -> bool:
    """Convenience wrapper — True iff at least one cited URL is grounded."""
    return validate_cited_sources(cited, searched).has_any_grounded_citation()




@dataclass(frozen=True)
class SearchedSource:
    """A single URL the web_search server tool actually retrieved.

    The verifier was previously collecting these as bare strings, which
    made it impossible to surface titles in reports without re-walking
    the assistant message. Chunk H stores the title alongside the URL so
    reports can render ``[title] (url)`` without losing data.
    """

    url: str
    title: str = ""

    @property
    def normalized(self) -> str:
        return normalize_url(self.url)


def dedupe_searched_sources(
    sources: Iterable[SearchedSource | dict | str | None],
) -> list[SearchedSource]:
    """Collapse equivalent searched URLs to a single record.

    Two retrieved URLs that normalize to the same canonical form are
    counted once. The first occurrence wins for the rendered URL/title
    so reports show the form the search tool actually returned. Inputs
    may be ``SearchedSource`` records, plain dicts ``{url, title}``, or
    bare strings — the helper accepts whatever the caller has handy.
    """
    seen: dict[str, SearchedSource] = {}
    ordered: list[SearchedSource] = []
    for raw in sources or []:
        if raw is None:
            continue
        if isinstance(raw, SearchedSource):
            record = raw
        elif isinstance(raw, dict):
            url = str(raw.get("url") or "")
            if not url:
                continue
            record = SearchedSource(url=url, title=str(raw.get("title") or ""))
        elif isinstance(raw, str):
            if not raw.strip():
                continue
            record = SearchedSource(url=raw, title="")
        else:
            continue
        key = record.normalized
        if not key:
            continue
        if key in seen:
            continue
        seen[key] = record
        ordered.append(record)
    return ordered
