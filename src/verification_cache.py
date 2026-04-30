"""Per-run verification result cache (plan section 7.2).

Caches verdicts by a normalized claim key so two findings that ask the same
external question only verify once. Crucially, the key includes more than
``codeReference`` alone — two findings that cite the same standard but make
different claims (e.g. "is current" vs "was withdrawn") still verify
separately.

Caches are constructed per pipeline run and discarded afterward. We do not
persist to disk: cached evidence ages quickly and persisting would require
the security-and-data-handling work scoped to Phase 6.
"""
from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .verifier import VerificationResult

from .code_cycles import CodeCycle


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    return _WHITESPACE_RE.sub(" ", text.strip().lower())


def _digest(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _claim_summary(finding) -> str:
    """Compact text capturing the claim of a finding for cache keying.

    The plan calls out that the cache must distinguish two findings that
    quote the same code reference but assert different things. We hash the
    issue text plus existing/replacement text so different claims diverge.
    """
    parts = [
        _normalize(getattr(finding, "issue", "")),
        _normalize(getattr(finding, "existingText", "")),
        _normalize(getattr(finding, "replacementText", "")),
    ]
    return "\n".join(p for p in parts if p)


def make_cache_key(finding, *, cycle: CodeCycle) -> str:
    """Build a stable cache key for a finding under a given code cycle."""
    code_ref = _normalize(getattr(finding, "codeReference", "")) or "_no_ref"
    action = _normalize(getattr(finding, "actionType", "")) or "_no_action"
    cycle_label = _normalize(getattr(cycle, "label", "")) or "_no_cycle"
    claim = _claim_summary(finding)
    return f"{cycle_label}|{action}|{code_ref}|{_digest(claim)}"


@dataclass
class VerificationCache:
    """Thread-safe per-run cache. Hits and misses are tracked for diagnostics."""
    _entries: dict[str, "VerificationResult"] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    hits: int = 0
    misses: int = 0

    def get(self, finding, *, cycle: CodeCycle) -> "VerificationResult | None":
        key = make_cache_key(finding, cycle=cycle)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            self.hits += 1
        return _clone_for_hit(entry)

    def put(self, finding, *, cycle: CodeCycle, result: "VerificationResult") -> None:
        # Don't cache results that explicitly opted out of caching, or
        # results that came from an unsuccessful local skip path. We only
        # want to share *grounded* verdicts across findings.
        if not getattr(result, "grounded", False):
            return
        key = make_cache_key(finding, cycle=cycle)
        with self._lock:
            # Store a snapshot with cache_status="miss" so subsequent gets()
            # can tag clones as hits without mutating shared state.
            stored = _clone_for_store(result)
            self._entries[key] = stored

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "hits": self.hits,
                "misses": self.misses,
                "size": len(self._entries),
            }


def _clone_for_store(result: "VerificationResult") -> "VerificationResult":
    from .verifier import VerificationResult
    return VerificationResult(
        verdict=result.verdict,
        explanation=result.explanation,
        sources=list(result.sources),
        correction=result.correction,
        grounded=result.grounded,
        model_used=result.model_used,
        escalated=result.escalated,
        cache_status="miss",
        web_search_requests=result.web_search_requests,
        successful_source_count=result.successful_source_count,
        search_error_count=result.search_error_count,
    )


def _clone_for_hit(stored: "VerificationResult") -> "VerificationResult":
    from .verifier import VerificationResult
    return VerificationResult(
        verdict=stored.verdict,
        explanation=stored.explanation,
        sources=list(stored.sources),
        correction=stored.correction,
        grounded=stored.grounded,
        model_used=stored.model_used,
        escalated=stored.escalated,
        cache_status="hit",
        web_search_requests=stored.web_search_requests,
        successful_source_count=stored.successful_source_count,
        search_error_count=stored.search_error_count,
    )
