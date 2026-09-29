"""Shared source resolution within one run (plan EX-04, part B).

Findings about the same authoritative material send the verifier after the
same passages: a review finding and a compliance finding that both cite
NFPA 13 §8.15.1 under one project's code basis each pay for their own
searches. This prototype hands a later verification the passages the API cited
while verifying an earlier finding **with the same claim context**, so the
verifier can check them before (or instead of) searching again. Off by
default (``SPEC_CRITIC_SOURCE_REUSE``, ``api_config``): ``shadow`` records
what would have been supplied and supplies nothing (requests unchanged, either
transport) — the free first measurement of how often contexts match at all;
``supply`` supplies it (real-time transport only).

**Retrieval is reused, never a verdict.** What travels is passages; every
finding still gets its own verification call, its own verdict, and (with the
validator on) its own support assessment. Verdict reuse between equivalent
claims is the verification cache's job, keyed by the claim itself — this
module never touches it.

**The key is the claim context, not a standard's name.** :class:`SourceContext`
holds every input that decides whether a passage retrieved for one finding is
evidence about another:

* the normalized **reference** — designators and section numbers from the
  finding's ``codeReference`` (``NFPA 13 §8.15.1``); a finding with no
  recognizable reference is not keyable and never reuses anything;
* the **editions** at issue — each designator's edition as the claim names
  it, else the module's pinned edition, else ``unpinned``;
* the **code cycle** (label plus the pinned-editions fingerprint the cache
  uses), so a pin correction starts a new context;
* the **jurisdiction** (the project profile's fingerprint) and the rendered
  **governing basis** fingerprint, so a passage retrieved under one adoption
  basis is never supplied under another;
* the **authority** the claim is asserted under — its verification profile
  (jurisdictional, code standard, manufacturer, constructability, internal
  coordination), so a manufacturer's datasheet retrieved for one claim is not
  supplied as code text for another;
* the **module**, and the **resolver policy version** (:data:`RESOLVER_POLICY_VERSION`).

The **client** is constant within a run (one project profile per run), so a
run-scoped store needs no client field; a store that outlived a run would
have to add one before anything could be reused across runs. **Freshness** is
checked at lookup rather than keyed: a source retrieved more than
``max_age_seconds`` ago is stale and is not supplied.

**What can be reused.** Only text the API itself extracted from a retrieved
page: the ``cited_text`` of a native citation whose source this conversation
retrieved (web search or web fetch). The verifier's own ``source_quote`` is
model-written and is never harvested, and a passage that was itself supplied
by this module (a ``search_result`` citation) is never harvested again, so a
reused passage can never come back labelled as a fresh retrieval.

**Lookup outcomes.** ``hit`` (fresh passages supplied); ``absent`` (nothing
recorded for this context); ``stale`` (recorded, all too old);
``incompatible`` (the same reference was resolved under a different context —
the differing fields are named); ``insufficient`` (recorded, no usable
passage); ``not_keyable`` (the finding names no recognizable reference). Every
outcome but ``hit`` falls back to fresh resolution: the verifier runs exactly
as it would with the switch off.

**How passages reach the verifier.** As ``search_result`` content blocks in
the user message, citations enabled (required when web search is in the same
request), followed by a note saying where they came from. The verifier may
cite them like a search result; its native citations then carry the
``search_result`` tool, and its result records the supplied URLs apart from
the ones it retrieved itself (``VerificationResult.reused_sources``).

Stdlib plus :mod:`reference_parsing` and :mod:`source_grounding`.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from . import native_citations as _native
from .reference_parsing import canonical_designator, designators, editions, normalize_reference
from .source_grounding import normalize_url

#: The semantics of :class:`SourceContext` and of what is harvested. Bump
#: when either changes, so nothing recorded under the old rules is supplied.
RESOLVER_POLICY_VERSION = "sr1"

#: A source retrieved longer ago than this is stale. Within one run the second
#: verification round follows the first by minutes on the real-time transport.
DEFAULT_MAX_AGE_SECONDS = 6 * 3600.0
MAX_SOURCES_PER_CONTEXT = 5
MAX_PASSAGES_PER_SOURCE = 4
MAX_PASSAGES_PER_REQUEST = 8
MAX_PASSAGE_CHARS = _native.MAX_CITED_TEXT_CHARS
MAX_CONTEXTS = 500

LOOKUP_HIT = "hit"
LOOKUP_ABSENT = "absent"
LOOKUP_STALE = "stale"
LOOKUP_INCOMPATIBLE = "incompatible"
LOOKUP_INSUFFICIENT = "insufficient"
LOOKUP_NOT_KEYABLE = "not_keyable"
#: ``shadow`` records what would be supplied and supplies nothing; ``supply``
#: supplies it (real-time transport only).
MODE_SHADOW = "shadow"
MODE_SUPPLY = "supply"

LOOKUP_STATUSES = (
    LOOKUP_HIT,
    LOOKUP_ABSENT,
    LOOKUP_STALE,
    LOOKUP_INCOMPATIBLE,
    LOOKUP_INSUFFICIENT,
    LOOKUP_NOT_KEYABLE,
)

#: The tools whose results are retrieval in the conversation that ran them.
_HARVESTABLE_TOOLS = frozenset({_native.TOOL_WEB_SEARCH, _native.TOOL_WEB_FETCH})
_RESOLVED = frozenset(
    {_native.RESOLUTION_DIRECT, _native.RESOLUTION_DOCUMENT_TEXT, _native.RESOLUTION_DOCUMENT_INDEX}
)

#: The note that follows the supplied passages in the user message.
REUSE_NOTE = (
    "<reused_passages>\n"
    "The search results above are passages the API cited while this run verified "
    "another finding that names the same reference, under the same code basis and "
    "jurisdiction. They were not retrieved for this finding. Follow your usual "
    "procedure. You may cite one of these passages, exactly as you would cite a web "
    "search result, where it bears on this finding's claim; anything they do not "
    "settle, check with web_search.\n"
    "</reused_passages>"
)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceContext:
    """Everything that decides whether a retrieved passage bears on a claim."""

    reference: tuple[str, ...]
    editions: tuple[tuple[str, str], ...]
    cycle: str
    jurisdiction: str
    basis: str
    authority: str
    module_id: str
    policy_version: str = RESOLVER_POLICY_VERSION

    def to_dict(self) -> dict:
        return {
            "reference": list(self.reference),
            "editions": [list(pair) for pair in self.editions],
            "cycle": self.cycle,
            "jurisdiction": self.jurisdiction,
            "basis": self.basis,
            "authority": self.authority,
            "module_id": self.module_id,
            "policy_version": self.policy_version,
        }

    def key(self) -> str:
        """A digest of every field: equal keys mean equal contexts."""
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]

    def differences(self, other: "SourceContext") -> list[str]:
        """The field names on which two contexts differ, in field order."""
        mine, theirs = self.to_dict(), other.to_dict()
        return [name for name in mine if mine[name] != theirs[name]]


def _pinned_editions(cycle: Any) -> dict[str, str]:
    """``{designator: edition}`` the cycle pins (standards and base codes)."""
    pins: dict[str, str] = {}
    for standard in getattr(cycle, "standards", ()) or ():
        names = designators(getattr(standard, "name", "") or "")
        edition = str(getattr(standard, "edition", "") or "").strip()
        if names and edition:
            pins.setdefault(names[0], edition)
    for base in getattr(cycle, "base_codes", ()) or ():
        name = str(getattr(base, "name", "") or "")
        names = designators(name) or ([canonical_designator(name)] if name.isupper() else [])
        year = str(getattr(base, "year", "") or "").strip()
        if names and year:
            pins.setdefault(names[0], year)
    asce7 = str(getattr(cycle, "asce7", "") or "").strip()
    if asce7:
        # ``"7-22"``: the edition is after the dash, written as the claim-side
        # reader writes it (four digits), so a claim naming ASCE 7-22 and one
        # relying on the pin share a context.
        edition = asce7.split("-", 1)[-1]
        if edition.isdigit() and len(edition) == 2:
            edition = str(2000 + int(edition) if int(edition) <= 40 else 1900 + int(edition))
        pins.setdefault("ASCE 7", edition)
    return pins


def _cycle_identity(cycle: Any) -> str:
    try:
        from .verification_cache import _standards_fingerprint

        fingerprint = _standards_fingerprint(cycle)
    except Exception:  # pragma: no cover - a key must never raise
        fingerprint = ""
    return f"{getattr(cycle, 'label', '') or ''}|{fingerprint}"


def _authority(finding: Any, cycle: Any) -> str:
    try:
        from ..modules import module_for_cycle
        from .verification_profiles import classify_finding_profile

        module = module_for_cycle(cycle)
        profile = classify_finding_profile(
            finding, keywords=getattr(module, "profile_keywords", None)
        )
        return str(getattr(profile, "value", profile))
    except Exception:  # pragma: no cover - a key must never raise
        return ""


def context_for(
    finding: Any,
    *,
    cycle: Any,
    module_id: str,
    jurisdiction_fingerprint: str | None = None,
    basis_fingerprint: str | None = None,
) -> SourceContext | None:
    """The claim context of ``finding``, or ``None`` when it is not keyable.

    Keyable means its ``codeReference`` names at least one designator this
    build recognizes (:mod:`reference_parsing`). A finding without one is
    never supplied reused passages.
    """
    code_reference = getattr(finding, "codeReference", "") or ""
    reference = normalize_reference(code_reference)
    if not reference:
        return None
    claim_text = "\n".join(
        str(getattr(finding, name, "") or "")
        for name in ("codeReference", "issue", "replacementText")
    )
    named = editions(claim_text)
    pins = _pinned_editions(cycle)
    pairs: list[tuple[str, str]] = []
    for name in designators(code_reference):
        if name in named:
            edition = "+".join(str(y) for y in sorted(named[name]))
        else:
            edition = pins.get(name, "unpinned")
        pairs.append((name, edition))
    return SourceContext(
        reference=reference,
        editions=tuple(sorted(pairs)),
        cycle=_cycle_identity(cycle),
        jurisdiction=jurisdiction_fingerprint or "",
        basis=basis_fingerprint or "",
        authority=_authority(finding, cycle),
        module_id=module_id or "",
    )


# ---------------------------------------------------------------------------
# Sources and harvest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedSource:
    """One retrieved source and the passages the API cited from it."""

    url: str
    title: str
    passages: tuple[str, ...]
    tool: str
    retrieved_at: float
    origin_finding_id: str = ""
    origin_attempt_id: str = ""
    origin_model: str = ""

    def to_dict(self, *, now: float | None = None) -> dict:
        record = {
            "url": self.url,
            "title": self.title,
            "passages": len(self.passages),
            "tool": self.tool,
            "origin_finding_id": self.origin_finding_id,
            "origin_attempt_id": self.origin_attempt_id,
            "origin_model": self.origin_model,
        }
        if now is not None:
            record["age_seconds"] = round(max(0.0, now - self.retrieved_at), 1)
        return record


def harvest_sources(result: Any, *, finding_id: str = "", now: float) -> list[ResolvedSource]:
    """The reusable sources a fresh verification retrieved, in citation order.

    Only a fresh result (``cache_status`` ``miss``) is harvested — a replay's
    passages were retrieved by another conversation at another time, and
    stamping them ``now`` would make an old retrieval look new. Only native
    citations whose source this conversation retrieved, by web search or web
    fetch, with cited text, are kept; a ``search_result`` citation (a passage
    this module supplied) is never harvested.
    """
    if (getattr(result, "cache_status", "") or "").strip() != "miss":
        return []
    records = getattr(result, "native_citations", None)
    if not isinstance(records, list):
        return []
    by_url: "OrderedDict[str, dict]" = OrderedDict()
    for record in records:
        if not isinstance(record, dict) or not record.get("recognized", False):
            continue
        if record.get("tool") not in _HARVESTABLE_TOOLS:
            continue
        if record.get("resolution") not in _RESOLVED or not record.get("retrieved", False):
            continue
        url = str(record.get("url") or "").strip()
        text = str(record.get("cited_text") or "").strip()
        key = normalize_url(url)
        if not key or not text:
            continue
        entry = by_url.setdefault(
            key,
            {
                "url": url,
                "title": str(record.get("title") or "").strip(),
                "passages": [],
                "tool": record.get("tool"),
                "attempt_id": str(record.get("attempt_id") or ""),
                "model": str(record.get("model") or ""),
            },
        )
        passage = text[:MAX_PASSAGE_CHARS]
        if passage not in entry["passages"] and len(entry["passages"]) < MAX_PASSAGES_PER_SOURCE:
            entry["passages"].append(passage)
    return [
        ResolvedSource(
            url=entry["url"],
            title=entry["title"],
            passages=tuple(entry["passages"]),
            tool=str(entry["tool"]),
            retrieved_at=float(now),
            origin_finding_id=finding_id,
            origin_attempt_id=entry["attempt_id"],
            origin_model=entry["model"],
        )
        for entry in list(by_url.values())[:MAX_SOURCES_PER_CONTEXT]
    ]


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReuseLookup:
    """What the store found for one finding's context.

    ``mode`` says what happens to a hit: under ``shadow`` it is recorded and
    nothing is supplied (:attr:`supplies` is False), so the request is exactly
    what it would be with the experiment off.
    """

    status: str
    context_key: str = ""
    sources: tuple[ResolvedSource, ...] = ()
    reason: str = ""
    looked_up_at: float = 0.0
    mode: str = MODE_SUPPLY

    @property
    def hit(self) -> bool:
        return self.status == LOOKUP_HIT and bool(self.sources)

    @property
    def supplies(self) -> bool:
        """True when this lookup's passages go into the request."""
        return self.hit and self.mode == MODE_SUPPLY

    def supplied_urls(self) -> list[str]:
        return [source.url for source in self.sources] if self.supplies else []

    def to_dict(self) -> dict:
        """Provenance for the result, the trace, and diagnostics (no passage text).

        ``sources`` lists what the store matched (on a hit, in either mode);
        ``supplied`` says whether it reached the request.
        """
        return {
            "policy_version": RESOLVER_POLICY_VERSION,
            "mode": self.mode,
            "status": self.status,
            "context_key": self.context_key,
            "reason": self.reason,
            "supplied": self.supplies,
            "sources": [source.to_dict(now=self.looked_up_at) for source in self.sources]
            if self.hit
            else [],
            "passages": sum(len(source.passages) for source in self.sources) if self.hit else 0,
        }


def not_keyable(mode: str = MODE_SUPPLY) -> ReuseLookup:
    return ReuseLookup(
        LOOKUP_NOT_KEYABLE,
        reason="the finding names no recognizable code or standard reference",
        mode=mode,
    )


@dataclass
class _Entry:
    context: SourceContext
    sources: "OrderedDict[str, ResolvedSource]" = field(default_factory=OrderedDict)


class SourceStore:
    """The run's reusable sources, keyed by :class:`SourceContext`.

    In memory only, one per run (``VerificationCache.source_store``), guarded
    by a lock because a routed program's modules collect concurrently. Bounded
    to :data:`MAX_CONTEXTS` contexts (the oldest is dropped first).
    """

    def __init__(
        self,
        *,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._max_age = float(max_age_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self._by_reference: dict[tuple[str, ...], set[str]] = {}

    def now(self) -> float:
        return float(self._clock())

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def record(self, context: SourceContext, sources: Iterable[ResolvedSource]) -> int:
        """Merge ``sources`` under ``context``; return how many sources it now holds."""
        sources = list(sources or [])
        if not sources:
            return 0
        key = context.key()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(context=context)
                self._entries[key] = entry
                self._by_reference.setdefault(context.reference, set()).add(key)
            for source in sources:
                url_key = normalize_url(source.url)
                existing = entry.sources.get(url_key)
                if existing is None:
                    if len(entry.sources) >= MAX_SOURCES_PER_CONTEXT:
                        continue
                    entry.sources[url_key] = source
                    continue
                passages = list(existing.passages)
                for passage in source.passages:
                    if passage not in passages and len(passages) < MAX_PASSAGES_PER_SOURCE:
                        passages.append(passage)
                entry.sources[url_key] = ResolvedSource(
                    url=existing.url,
                    title=existing.title or source.title,
                    passages=tuple(passages),
                    tool=existing.tool,
                    retrieved_at=max(existing.retrieved_at, source.retrieved_at),
                    origin_finding_id=existing.origin_finding_id,
                    origin_attempt_id=existing.origin_attempt_id,
                    origin_model=existing.origin_model,
                )
            self._entries.move_to_end(key)
            while len(self._entries) > MAX_CONTEXTS:
                old_key, old = self._entries.popitem(last=False)
                keys = self._by_reference.get(old.context.reference)
                if keys is not None:
                    keys.discard(old_key)
                    if not keys:
                        self._by_reference.pop(old.context.reference, None)
            return len(entry.sources)

    def harvest(self, context: SourceContext | None, result: Any, *, finding_id: str = "") -> int:
        """Record what a fresh verification retrieved under its finding's context."""
        if context is None:
            return 0
        return self.record(context, harvest_sources(result, finding_id=finding_id, now=self.now()))

    def lookup(self, context: SourceContext | None, *, mode: str = MODE_SUPPLY) -> ReuseLookup:
        """What this run can supply for ``context`` (see the module docstring)."""
        if context is None:
            return not_keyable(mode)
        now = self.now()
        key = context.key()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                others = [
                    self._entries[k].context
                    for k in sorted(self._by_reference.get(context.reference, ()))
                    if k in self._entries
                ]
                if others:
                    fields = sorted({name for other in others for name in context.differences(other)})
                    return ReuseLookup(
                        LOOKUP_INCOMPATIBLE,
                        context_key=key,
                        reason="the same reference was resolved under a different "
                        + ", ".join(fields),
                        looked_up_at=now,
                        mode=mode,
                    )
                return ReuseLookup(
                    LOOKUP_ABSENT,
                    context_key=key,
                    reason="nothing was retrieved for this context earlier in the run",
                    looked_up_at=now,
                    mode=mode,
                )
            sources = list(entry.sources.values())
        usable = [s for s in sources if s.passages]
        if not usable:
            return ReuseLookup(
                LOOKUP_INSUFFICIENT,
                context_key=key,
                reason="sources were recorded for this context but none has a cited passage",
                looked_up_at=now,
                mode=mode,
            )
        fresh = [s for s in usable if now - s.retrieved_at <= self._max_age]
        if not fresh:
            return ReuseLookup(
                LOOKUP_STALE,
                context_key=key,
                reason=f"every recorded source is older than {self._max_age:g} s",
                looked_up_at=now,
                mode=mode,
            )
        supplied: list[ResolvedSource] = []
        budget = MAX_PASSAGES_PER_REQUEST
        for source in fresh:
            if budget <= 0:
                break
            passages = source.passages[:budget]
            budget -= len(passages)
            supplied.append(
                ResolvedSource(
                    url=source.url,
                    title=source.title,
                    passages=passages,
                    tool=source.tool,
                    retrieved_at=source.retrieved_at,
                    origin_finding_id=source.origin_finding_id,
                    origin_attempt_id=source.origin_attempt_id,
                    origin_model=source.origin_model,
                )
            )
        return ReuseLookup(
            LOOKUP_HIT, context_key=key, sources=tuple(supplied), looked_up_at=now, mode=mode
        )


def source_reuse_record(lookup: ReuseLookup, result: Any) -> dict:
    """The lookup's provenance plus what the kept verdict did with it.

    Stamped on the result as ``source_reuse``: by ``verify_finding`` in
    ``supply`` mode, by the pipeline after the round in ``shadow`` mode.
    ``supplied_to`` names the pass that received the passages (only ever the
    initial pass); ``kept_pass`` the pass whose verdict was kept.
    ``accepted_via_reuse`` lists the kept verdict's accepted citations that
    only a supplied passage could have validated — none of this
    conversation's own searched or fetched pages is the same page.
    """
    record = lookup.to_dict()
    supplied = {normalize_url(u) for u in lookup.supplied_urls()}
    own = {
        normalize_url(u)
        for u in [
            *(getattr(result, "searched_sources", None) or []),
            *(getattr(result, "fetched_sources", None) or []),
        ]
    }
    record["supplied_to"] = "initial" if lookup.supplies else ""
    record["kept_pass"] = "escalation" if bool(getattr(result, "escalated", False)) else "initial"
    record["accepted_via_reuse"] = [
        u
        for u in (getattr(result, "accepted_sources", None) or [])
        if normalize_url(u) in supplied and normalize_url(u) not in own
    ]
    return record


# ---------------------------------------------------------------------------
# Request content
# ---------------------------------------------------------------------------


def search_result_blocks(lookup: ReuseLookup | None) -> list[dict]:
    """The supplied passages as ``search_result`` content blocks.

    ``[]`` unless the lookup supplies (a hit in ``supply`` mode).
    """
    if lookup is None or not lookup.supplies:
        return []
    return [
        {
            "type": "search_result",
            "source": source.url,
            "title": source.title or source.url,
            "content": [{"type": "text", "text": passage} for passage in source.passages],
            # Required when web_search is in the same request, and all-or-none
            # across a request's search results.
            "citations": {"enabled": True},
        }
        for source in lookup.sources
    ]


def user_content(prompt: str, lookup: ReuseLookup | None) -> str | list[dict]:
    """The verification user message: ``prompt`` unchanged unless it supplies.

    With a supplying hit: the ``search_result`` blocks, :data:`REUSE_NOTE`, then the
    prompt, each its own block. Without one the string itself, so the request
    is byte-identical to a run with the switch off.
    """
    blocks = search_result_blocks(lookup)
    if not blocks:
        return prompt
    return [*blocks, {"type": "text", "text": REUSE_NOTE}, {"type": "text", "text": prompt}]


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def compact_record(record: Any, *, web_search_requests: int = 0) -> dict | None:
    """A result's ``source_reuse`` record as a diagnostics event carries it."""
    if not isinstance(record, dict):
        return None
    return {
        "policy_version": record.get("policy_version", ""),
        "mode": record.get("mode", ""),
        "status": record.get("status", ""),
        "supplied": bool(record.get("supplied", False)),
        "reason": record.get("reason", ""),
        "passages": int(record.get("passages", 0) or 0),
        "sources": len(record.get("sources") or []),
        "accepted_via_reuse": len(record.get("accepted_via_reuse") or []),
        "kept_pass": record.get("kept_pass", ""),
        "web_search_requests": int(web_search_requests or 0),
    }


def summarize_reuse(records: Iterable[Any]) -> dict | None:
    """A run's rollup of compact reuse records, or ``None`` when there are none.

    A record may carry the diagnostics ``phase`` it was logged under
    (``verification`` for the first round, ``cross_check_verification`` for
    the second); ``by_phase`` keeps each round apart, because a first-round
    lookup can never match (the store is empty until that round ends) and
    pooling the rounds would understate the second round's match rate.

    The search counts are telemetry, not a comparison: findings supplied
    passages and findings not supplied any differ in more than that, so only
    the controlled comparison in ``evals/evidence_validation.py`` can say
    whether reuse saved searches.
    """
    records = [r for r in records or [] if isinstance(r, dict)]
    if not records:
        return None
    by_status: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    by_phase: dict[str, dict] = {}
    supplied = accepted = passages = matched_in_shadow = shadow_passages = 0
    searches_with = searches_without = 0
    for record in records:
        status = str(record.get("status") or "")
        by_status[status] = by_status.get(status, 0) + 1
        mode = str(record.get("mode") or "")
        by_mode[mode] = by_mode.get(mode, 0) + 1
        phase = by_phase.setdefault(
            str(record.get("phase") or ""),
            {"lookups": 0, "matched": 0, "passages_matched": 0, "by_status": {}},
        )
        phase["lookups"] += 1
        phase["by_status"][status] = phase["by_status"].get(status, 0) + 1
        if status == LOOKUP_HIT:
            phase["matched"] += 1
            phase["passages_matched"] += int(record.get("passages", 0) or 0)
        if record.get("supplied"):
            supplied += 1
            passages += int(record.get("passages", 0) or 0)
            searches_with += int(record.get("web_search_requests", 0) or 0)
            if record.get("accepted_via_reuse"):
                accepted += 1
        else:
            if status == LOOKUP_HIT:
                matched_in_shadow += 1
                shadow_passages += int(record.get("passages", 0) or 0)
            searches_without += int(record.get("web_search_requests", 0) or 0)
    return {
        "policy_version": RESOLVER_POLICY_VERSION,
        "lookups": len(records),
        "by_mode": by_mode,
        "by_status": by_status,
        "by_phase": by_phase,
        "findings_matched_in_shadow": matched_in_shadow,
        "passages_matched_in_shadow": shadow_passages,
        "findings_supplied": supplied,
        "passages_supplied": passages,
        "verdicts_accepting_reused_source": accepted,
        "web_search_requests_when_supplied": searches_with,
        "web_search_requests_when_not_supplied": searches_without,
    }


def summary_line(rollup: dict | None) -> str:
    """One line for the diagnostics text export ("" when there is no rollup)."""
    if not rollup:
        return ""
    statuses = ", ".join(f"{k} {v}" for k, v in sorted((rollup.get("by_status") or {}).items()))
    return (
        f"Source reuse (experiment, {rollup.get('policy_version', '')}): "
        f"{rollup.get('lookups', 0)} lookup(s) ({statuses}); "
        f"{rollup.get('findings_matched_in_shadow', 0)} matched in shadow; "
        f"{rollup.get('passages_supplied', 0)} passage(s) supplied to "
        f"{rollup.get('findings_supplied', 0)} finding(s); "
        f"{rollup.get('verdicts_accepting_reused_source', 0)} kept verdict(s) "
        "accepted a supplied source"
    )
