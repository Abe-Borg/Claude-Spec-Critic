"""Persistent verification result cache (plan section 7.2).

Caches verdicts by a normalized claim key so two findings that ask the same
external question only verify once. Crucially, the key includes more than
``codeReference`` alone — two findings that cite the same standard but make
different claims (e.g. "is current" vs "was withdrawn") still verify
separately.

Phase 10: the cache persists to disk between runs. Cycle label is part of
the key, so switching code cycles naturally invalidates everything from the
prior cycle — no calendar TTL is required for correctness. An optional
``SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS`` environment override is provided
for users who want age-based pruning anyway; the default (0) is a database,
not a cache.

Only ``grounded=True`` results are stored, preserving the existing safety
guarantee that cached verdicts are always backed by external evidence.

The verifier model is intentionally omitted from the cache key. Cache entries
represent grounded verdict semantics for a finding/cycle/action/claim, not the
particular model that produced the verdict. ``model_used`` is still persisted
as entry provenance for reports and future maintenance tools, but changing
``SPEC_CRITIC_VERIFICATION_MODEL`` or ``SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL``
does not invalidate existing hits; clear the cache file (see
``default_cache_path``) or set ``SPEC_CRITIC_CACHE_PATH`` to a fresh file to
force re-verification with a new model policy.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .verifier import VerificationResult

from .code_cycles import CodeCycle


_WHITESPACE_RE = re.compile(r"\s+")

# JSON schema version for the on-disk cache file. Bumped when the entry
# shape changes incompatibly so older readers can refuse to load instead of
# silently mis-deserializing.
_CACHE_SCHEMA_VERSION = 1


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
    """Build a stable cache key for a finding under a given code cycle.

    The key includes the normalized cycle label, action type, code reference,
    and a digest of the finding claim summary. It intentionally does *not*
    include the verifier model: the cache is keyed by the grounded verification
    question and code-cycle semantics, while ``VerificationResult.model_used``
    is stored only as provenance. Changing verifier models therefore reuses
    compatible grounded cache entries; delete ``default_cache_path()`` (or point
    ``SPEC_CRITIC_CACHE_PATH`` at a new file) when a fresh model pass is
    required.
    """
    code_ref = _normalize(getattr(finding, "codeReference", "")) or "_no_ref"
    action = _normalize(getattr(finding, "actionType", "")) or "_no_action"
    cycle_label = _normalize(getattr(cycle, "label", "")) or "_no_cycle"
    claim = _claim_summary(finding)
    return f"{cycle_label}|{action}|{code_ref}|{_digest(claim)}"


def cache_persist_enabled() -> bool:
    """Whether verification cache should persist to disk between runs."""
    return os.environ.get("SPEC_CRITIC_VERIFICATION_CACHE_PERSIST", "1") != "0"


def cache_ttl_days() -> int:
    """Optional age-based pruning. 0 (default) means no expiry."""
    raw = os.environ.get("SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS", "0").strip()
    try:
        days = int(raw)
    except ValueError:
        days = 0
    return max(0, days)


def default_cache_path() -> Path:
    """Resolve the on-disk cache file path.

    Honors ``SPEC_CRITIC_CACHE_PATH`` for explicit overrides; otherwise
    defaults to ``~/.spec_critic/verification_cache.json``.
    """
    override = os.environ.get("SPEC_CRITIC_CACHE_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".spec_critic" / "verification_cache.json"


@dataclass
class _CacheEntry:
    """Stored verdict with sidecar metadata for future maintenance tools."""
    result: "VerificationResult"
    created_ts: float


@dataclass
class VerificationCache:
    """Thread-safe cache shared across a pipeline run.

    Per-run hits/misses are tracked in memory for diagnostics. Persistent
    metadata (creation timestamp per entry) is preserved across save/load
    so an external maintenance tool can prune by age or model version.
    """
    _entries: dict[str, _CacheEntry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    hits: int = 0
    misses: int = 0
    loaded_from_disk: int = 0
    expired_on_load: int = 0

    def get(self, finding, *, cycle: CodeCycle) -> "VerificationResult | None":
        key = make_cache_key(finding, cycle=cycle)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            self.hits += 1
        return _clone_for_hit(entry.result)

    def put(self, finding, *, cycle: CodeCycle, result: "VerificationResult") -> None:
        # Don't cache results that explicitly opted out of caching, or
        # results that came from an unsuccessful local skip path. We only
        # want to share *grounded* verdicts across findings.
        if not getattr(result, "grounded", False):
            return
        key = make_cache_key(finding, cycle=cycle)
        with self._lock:
            stored = _clone_for_store(result)
            self._entries[key] = _CacheEntry(result=stored, created_ts=time.time())

    def stats(self) -> dict[str, int]:
        with self._lock:
            oldest_ts = min((e.created_ts for e in self._entries.values()), default=0.0)
            return {
                "hits": self.hits,
                "misses": self.misses,
                "size": len(self._entries),
                "loaded_from_disk": self.loaded_from_disk,
                "expired_on_load": self.expired_on_load,
                "oldest_entry_ts": int(oldest_ts) if oldest_ts else 0,
            }

    # ------------------------------------------------------------------
    # Disk persistence
    # ------------------------------------------------------------------

    def load_from_disk(self, path: str | Path | None = None) -> int:
        """Load entries from a JSON cache file.

        Returns the number of entries loaded. Silent on missing file —
        first-run users have no cache yet, and that is a normal state.
        Corrupt or schema-mismatched files are skipped with the in-memory
        cache left empty rather than crashing the run.

        Honors the optional TTL: entries older than
        ``SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS`` are dropped on load.
        """
        target = Path(path) if path is not None else default_cache_path()
        if not target.exists():
            return 0
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        if not isinstance(payload, dict):
            return 0
        if int(payload.get("version", 0) or 0) != _CACHE_SCHEMA_VERSION:
            return 0
        raw_entries = payload.get("entries") or {}
        if not isinstance(raw_entries, dict):
            return 0

        ttl_days = cache_ttl_days()
        cutoff = time.time() - (ttl_days * 86400) if ttl_days > 0 else 0.0
        loaded = 0
        expired = 0
        from .verifier import VerificationResult

        with self._lock:
            for key, raw in raw_entries.items():
                if not isinstance(raw, dict):
                    continue
                created_ts = float(raw.get("created_ts") or 0.0)
                if cutoff and created_ts and created_ts < cutoff:
                    expired += 1
                    continue
                result_payload = raw.get("result")
                if not isinstance(result_payload, dict):
                    continue
                try:
                    raw_rejected = result_payload.get("rejected_sources") or []
                    rejected: list[dict] = []
                    for r in raw_rejected:
                        if isinstance(r, dict):
                            rejected.append(
                                {
                                    "url": str(r.get("url") or ""),
                                    "reason": str(r.get("reason") or ""),
                                }
                            )
                    entry_result = VerificationResult(
                        verdict=str(result_payload.get("verdict") or "UNVERIFIED"),
                        explanation=str(result_payload.get("explanation") or ""),
                        sources=[str(s) for s in (result_payload.get("sources") or []) if s],
                        correction=(
                            str(result_payload["correction"])
                            if result_payload.get("correction") is not None
                            else None
                        ),
                        grounded=bool(result_payload.get("grounded", False)),
                        model_used=str(result_payload.get("model_used") or ""),
                        escalated=bool(result_payload.get("escalated", False)),
                        cache_status="miss",
                        web_search_requests=int(result_payload.get("web_search_requests", 0) or 0),
                        successful_source_count=int(
                            result_payload.get("successful_source_count", 0) or 0
                        ),
                        search_error_count=int(result_payload.get("search_error_count", 0) or 0),
                        # Chunk H source-grounding evidence. Pre-Chunk-H
                        # entries lack these keys; the defaults below
                        # preserve the cached verdict exactly.
                        searched_sources=[
                            str(s) for s in (result_payload.get("searched_sources") or []) if s
                        ],
                        cited_sources=[
                            str(s) for s in (result_payload.get("cited_sources") or []) if s
                        ],
                        accepted_sources=[
                            str(s) for s in (result_payload.get("accepted_sources") or []) if s
                        ],
                        rejected_sources=rejected,
                        verification_profile=str(
                            result_payload.get("verification_profile") or ""
                        ),
                        # Chunk I: stored verification mode. Missing on
                        # pre-Chunk-I entries — defaults to "" so the
                        # routing logic falls back to current behavior
                        # the next time the entry is used.
                        verification_mode=str(
                            result_payload.get("verification_mode") or ""
                        ),
                    )
                except Exception:
                    continue
                if not entry_result.grounded:
                    # Defensive: only grounded entries should ever be on
                    # disk, but reject any that slipped in.
                    continue
                self._entries[key] = _CacheEntry(
                    result=entry_result,
                    created_ts=created_ts or time.time(),
                )
                loaded += 1
            self.loaded_from_disk = loaded
            self.expired_on_load = expired
        return loaded

    def save_to_disk(self, path: str | Path | None = None) -> int:
        """Atomically write the cache to JSON.

        Returns the number of entries written. Atomic via temp-file +
        rename so a crash mid-write cannot corrupt an existing cache file.
        """
        target = Path(path) if path is not None else default_cache_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            entries_payload = {
                key: {
                    "created_ts": entry.created_ts,
                    "result": _result_to_dict(entry.result),
                }
                for key, entry in self._entries.items()
            }
            count = len(entries_payload)
        payload = {
            "version": _CACHE_SCHEMA_VERSION,
            "saved_at": time.time(),
            "entries": entries_payload,
        }
        # Atomic write: temp file in the same directory + rename.
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=".verification_cache.",
            suffix=".tmp",
            dir=str(target.parent),
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, indent=2)
            os.replace(tmp_name, target)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return count


def _result_to_dict(result: "VerificationResult") -> dict:
    return {
        "verdict": result.verdict,
        "explanation": result.explanation,
        "sources": list(result.sources),
        "correction": result.correction,
        "grounded": bool(result.grounded),
        "model_used": result.model_used,
        "escalated": bool(result.escalated),
        "web_search_requests": int(result.web_search_requests),
        "successful_source_count": int(result.successful_source_count),
        "search_error_count": int(result.search_error_count),
        # Chunk H: persist the source-grounding partition + profile so a
        # restored cache hit still shows reports the same accepted /
        # rejected sources the original run produced.
        "searched_sources": list(result.searched_sources),
        "cited_sources": list(result.cited_sources),
        "accepted_sources": list(result.accepted_sources),
        "rejected_sources": [dict(r) for r in result.rejected_sources],
        "verification_profile": result.verification_profile,
        # Chunk I: persist the verification mode so a restored cache hit
        # carries the original routing decision into reports and
        # diagnostics. Pre-Chunk-I entries (without this field) load as
        # empty string; the routing logic treats that as STANDARD_REASONING.
        "verification_mode": result.verification_mode,
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
        searched_sources=list(result.searched_sources),
        cited_sources=list(result.cited_sources),
        accepted_sources=list(result.accepted_sources),
        rejected_sources=[dict(r) for r in result.rejected_sources],
        verification_profile=result.verification_profile,
        verification_mode=result.verification_mode,
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
        searched_sources=list(stored.searched_sources),
        cited_sources=list(stored.cited_sources),
        accepted_sources=list(stored.accepted_sources),
        rejected_sources=[dict(r) for r in stored.rejected_sources],
        verification_profile=stored.verification_profile,
        verification_mode=stored.verification_mode,
    )
