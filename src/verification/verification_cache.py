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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .verifier import VerificationResult

from ..core.code_cycles import CodeCycle


_WHITESPACE_RE = re.compile(r"\s+")

# JSON schema version for the on-disk cache file. Bumped when the entry
# shape changes incompatibly so older readers can refuse to load instead of
# silently mis-deserializing.
#
# v2 — invalidates pre-v2 entries that may have stored a CONFIRMED/CORRECTED
# verdict without an accepted external citation. The strengthened
# :func:`src.verifier._enforce_grounding_invariant` would now downgrade
# those verdicts, so silently reusing them would let the old behavior
# leak through. Bumping the version drops every v1 cache file on first
# load; users get fresh verifications under the new invariant.
#
# v3 — Chunk 2 / Trust Upgrade. Adds ``source_quote`` (the verbatim snippet
# the model said it read) to every entry. v2 entries don't carry the
# quote, so silently reusing them would produce CONFIRMED / CORRECTED
# hits whose report rendering has no source_quote to show — a regression
# against the new invariant. Bumping the version drops every v2 cache
# file on first load; users get fresh verifications under the new shape.
_CACHE_SCHEMA_VERSION = 3

# Cache-key claim digest length (hex chars). 24 hex chars = 96 bits of entropy,
# enough that two distinct claims colliding is astronomically unlikely even
# across a corpus of millions of findings; the previous 16 hex chars / 64-bit
# digest was thin under birthday-bound math (50%+ collision risk at ~5B keys
# and observable collision risk at ~1M). Lookups that present an old 16-char
# digest will simply miss in the new cache, which is the safe failure mode —
# the next verification call re-grounds the claim and writes a 24-char entry.
# Going higher (32+) would add latency and disk-write bytes for diminishing
# safety; 24 hex chars is the deliberate sweet spot.
_CLAIM_DIGEST_LEN = 24


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    return _WHITESPACE_RE.sub(" ", text.strip().lower())


def _digest(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_CLAIM_DIGEST_LEN]


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


# Canonical "disable" tokens for boolean env-var flags. Anything else —
# including an unset variable — leaves the default-enabled behavior in place.
_DISABLE_TOKENS = frozenset({"0", "false", "no", "off"})


def _env_flag_disabled(name: str) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() in _DISABLE_TOKENS


def cache_persist_enabled() -> bool:
    """Whether verification cache should persist to disk between runs.

    Enabled by default. Set ``SPEC_CRITIC_VERIFICATION_CACHE_PERSIST=0`` to
    keep the cache in-memory only — useful for one-off runs and tests that
    don't want to touch the user's on-disk cache.
    """
    return not _env_flag_disabled("SPEC_CRITIC_VERIFICATION_CACHE_PERSIST")


_DEFAULT_CACHE_TTL_DAYS = 60


def cache_ttl_days() -> int:
    """Age-based pruning in days. Default 60 days.

    Chunk 5 / Trust Upgrade: the default is 60 days, balancing reuse
    against staleness for code references that may have new amendments
    or interpretations published quarterly. A cached "current edition is
    NFPA 13-2022" verdict older than two months may be wrong if the
    California Building Standards Commission adopted a newer edition in
    the interim — re-verification at that age catches the drift.

    Override via ``SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS``. Explicit
    ``0`` restores the legacy "no expiry" behavior for operators who
    want the cache to act as a permanent database. Malformed or
    negative values fall back to the 60-day default so a typo never
    accidentally invalidates the entire cache or disables expiry.
    """
    raw = os.environ.get("SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS")
    if raw is None or not raw.strip():
        return _DEFAULT_CACHE_TTL_DAYS
    try:
        value = int(raw.strip())
    except ValueError:
        return _DEFAULT_CACHE_TTL_DAYS
    # ``0`` is an explicit operator override meaning "no expiry"; preserve
    # the legacy semantics. Negative values are nonsensical — fall back
    # to the default rather than silently disabling expiry.
    if value == 0:
        return 0
    if value < 0:
        return _DEFAULT_CACHE_TTL_DAYS
    return value


def default_cache_path() -> Path:
    """Return the on-disk cache file path.

    Overridable via ``SPEC_CRITIC_CACHE_PATH``. The default is
    ``~/.spec_critic/verification_cache.json``. ``~`` and environment
    variables in the override are expanded so users can point at e.g.
    ``$XDG_CACHE_HOME/spec_critic/cache.json``.
    """
    override = os.environ.get("SPEC_CRITIC_CACHE_PATH")
    if override and override.strip():
        return Path(os.path.expandvars(os.path.expanduser(override.strip())))
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
        return _clone_for_hit(entry)

    def put(self, finding, *, cycle: CodeCycle, result: "VerificationResult") -> None:
        # Don't cache results that explicitly opted out of caching, or
        # results that came from an unsuccessful local skip path. We only
        # want to share *grounded* verdicts across findings.
        if not getattr(result, "grounded", False):
            return
        # Chunk 3 / Trust Upgrade: refuse to cache operational-failure
        # results. The ``verification_failed`` sentinel marks UNVERIFIED
        # results that came from a transient cause (rate limit, server
        # error, network failure, parse error, INVALID_REQUEST,
        # BATCH_CANCELED). Caching these would freeze the transient
        # error into a durable verdict and silently suppress
        # re-verification on later runs. The ``grounded`` guard above
        # already drops every UNVERIFIED, so in practice this branch is
        # defense-in-depth against a future call site that constructs a
        # grounded+failed result directly.
        if bool(getattr(result, "verification_failed", False)):
            return
        # Chunk 13 / Trust Upgrade: refuse to cache budget-exhausted
        # results. The ``budget_exhausted`` sentinel marks UNVERIFIED
        # outcomes where the verifier consumed its full mode-scaled
        # web_search budget without producing a grounded verdict.
        # Persisting these would freeze a transient evidence-shortfall
        # into a permanent UNVERIFIED — but the same finding might
        # ground on a re-run that allocates more budget (e.g. severity
        # was raised) or after the underlying source becomes
        # discoverable. Same defense-in-depth rationale as
        # ``verification_failed``: ``budget_exhausted=True`` implies
        # ``verdict=UNVERIFIED`` which the grounded guard above
        # already drops; this branch protects against a future call
        # site that constructs a grounded+exhausted result directly.
        if bool(getattr(result, "budget_exhausted", False)):
            return
        # Refuse to cache a CONFIRMED/CORRECTED that lacks any accepted
        # external citation. The verifier's
        # ``_enforce_grounding_invariant`` would have downgraded such a
        # result to UNVERIFIED before reaching here; this is defense in
        # depth against a test or future call site that puts directly.
        verdict_upper = (getattr(result, "verdict", "") or "").strip().upper()
        if verdict_upper in ("CONFIRMED", "CORRECTED") and not (
            getattr(result, "accepted_sources", None) or getattr(result, "sources", None)
        ):
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
                    # Single deserialization path — same allow-list +
                    # defensive coercion the in-memory clones use. Legacy
                    # entries that predate a telemetry field load it at its
                    # default (e.g. fetch / disagreement keys → 0 / False / []).
                    entry_result = _result_from_dict(result_payload, cache_status="miss")
                except Exception:
                    continue
                if not entry_result.grounded:
                    # Defensive: only grounded entries should ever be on
                    # disk, but reject any that slipped in.
                    continue
                # Belt-and-suspenders against a v2 entry that somehow
                # shipped without an accepted citation — silently
                # reusing it would power a source-less CONFIRMED on a
                # cache hit. Mirrors the invariant in
                # :func:`src.verifier._enforce_grounding_invariant`.
                verdict_upper = (entry_result.verdict or "").strip().upper()
                if verdict_upper in ("CONFIRMED", "CORRECTED") and not (
                    entry_result.accepted_sources or entry_result.sources
                ):
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


# ---------------------------------------------------------------------------
# VerificationResult serialization policy
# ---------------------------------------------------------------------------
#
# The cache persists only a *subset* of VerificationResult's fields — the
# durable verdict semantics plus the evidence/telemetry needed to re-render a
# cached hit identically. ``_PERSISTED_*`` below is the single source of truth
# for that subset: ``_result_to_dict`` and ``_result_from_dict`` both drive
# off it, replacing the four hand-maintained field-by-field projections this
# module used to carry (to-dict, from-dict on load, clone-for-store,
# clone-for-hit). A field is split by JSON type only so the loader can coerce
# legacy / hand-edited entries defensively; the persisted *names* are the
# combined set.
#
# Every field NOT persisted is listed in ``_SKIPPED_FIELDS`` with the reason.
# ``test_verification_cache_serialization`` asserts that
# ``_PERSISTED_FIELDS | _SKIPPED_FIELDS`` covers every dataclass field, so
# adding a field to VerificationResult fails the test until it is explicitly
# classified here — the drift that this unification exists to prevent.

# ``verdict`` defaults to "UNVERIFIED"; every other string field defaults to "".
_PERSISTED_STR_FIELDS = (
    "verdict",
    "explanation",
    "model_used",
    "verification_profile",
    "verification_mode",
    "source_quote",
)
_PERSISTED_BOOL_FIELDS = ("grounded", "escalated", "models_disagreed")
_PERSISTED_INT_FIELDS = (
    "web_search_requests",
    "successful_source_count",
    "search_error_count",
    "web_fetch_requests",
)
_PERSISTED_STR_LIST_FIELDS = (
    "sources",
    "searched_sources",
    "cited_sources",
    "accepted_sources",
    "fetched_sources",
    "initial_sources",
)
# ``correction`` (str | None) and ``rejected_sources`` (list[dict]) need
# bespoke coercion, so they sit outside the typed tuples above.
_PERSISTED_FIELD_ORDER = (
    *_PERSISTED_STR_FIELDS,
    "correction",
    *_PERSISTED_BOOL_FIELDS,
    *_PERSISTED_INT_FIELDS,
    *_PERSISTED_STR_LIST_FIELDS,
    "rejected_sources",
)
_PERSISTED_FIELDS = frozenset(_PERSISTED_FIELD_ORDER)

# Fields the cache deliberately does NOT persist, each with its reason. Kept
# as an explicit set (not an implicit omission) so the round-trip test can
# prove the union with _PERSISTED_FIELDS is exhaustive.
_SKIPPED_FIELDS = frozenset({
    # Replay state — stamped fresh on every store ("miss") / hit ("hit").
    "cache_status",
    "cache_entry_created_ts",
    # Raw in-memory payloads — diagnostics only, never persisted.
    "structured_payload",
    "retry_telemetry",
    # Transient signals the cache refuses to store (see ``put``): a re-run
    # must re-attempt these rather than replay a frozen shortfall.
    "verification_failed",
    "budget_exhausted",
    # Local-skip-only telemetry; local-skip results are never grounded, so
    # they never reach the cache in the first place.
    "requires_elevated_confidence",
    # Escalation history. A cache hit replays the final verdict and the
    # Chunk 12 ``models_disagreed`` / ``initial_sources`` signal, but not
    # the full before/after escalation trace — preserved behavior from the
    # original projections.
    "escalation_attempted",
    "initial_model",
    "initial_verdict",
    "escalation_changed_verdict",
    "escalation_reason",
    # Operational token counts — diagnostics only, not persisted.
    "input_tokens",
    "output_tokens",
})


def _coerce_rejected(raw) -> list[dict]:
    out: list[dict] = []
    for r in raw or []:
        if isinstance(r, dict):
            out.append(
                {"url": str(r.get("url") or ""), "reason": str(r.get("reason") or "")}
            )
    return out


def _result_to_dict(result: "VerificationResult") -> dict:
    """Project a VerificationResult onto its persisted-field dict.

    Uses :func:`dataclasses.asdict` (which deep-copies nested lists / dicts)
    and filters to the explicit ``_PERSISTED_FIELD_ORDER`` allow-list, so a
    newly added dataclass field is never silently written to disk — it has to
    be classified in ``_PERSISTED_*`` or ``_SKIPPED_FIELDS`` first.
    """
    full = asdict(result)
    return {name: full[name] for name in _PERSISTED_FIELD_ORDER}


def _result_from_dict(
    payload: dict,
    *,
    cache_status: str,
    cache_entry_created_ts: float = 0.0,
) -> "VerificationResult":
    """Rebuild a VerificationResult from a persisted-field dict.

    The inverse of :func:`_result_to_dict`, driven by the same allow-list.
    Coerces defensively so legacy / hand-edited cache files (missing keys,
    wrong JSON types) load to the field defaults rather than crashing. Skipped
    fields take their dataclass defaults; ``cache_status`` /
    ``cache_entry_created_ts`` are stamped by the caller (store vs. hit).
    """
    from .verifier import VerificationResult

    kwargs: dict = {}
    for name in _PERSISTED_STR_FIELDS:
        default = "UNVERIFIED" if name == "verdict" else ""
        kwargs[name] = str(payload.get(name) or default)
    for name in _PERSISTED_BOOL_FIELDS:
        kwargs[name] = bool(payload.get(name, False))
    for name in _PERSISTED_INT_FIELDS:
        kwargs[name] = int(payload.get(name, 0) or 0)
    for name in _PERSISTED_STR_LIST_FIELDS:
        kwargs[name] = [str(s) for s in (payload.get(name) or []) if s]
    kwargs["correction"] = (
        str(payload["correction"]) if payload.get("correction") is not None else None
    )
    kwargs["rejected_sources"] = _coerce_rejected(payload.get("rejected_sources"))
    return VerificationResult(
        cache_status=cache_status,
        cache_entry_created_ts=cache_entry_created_ts,
        **kwargs,
    )


def _clone_for_store(result: "VerificationResult") -> "VerificationResult":
    """In-memory store clone — round-trips through the persisted-field policy."""
    return _result_from_dict(_result_to_dict(result), cache_status="miss")


def _clone_for_hit(entry: _CacheEntry) -> "VerificationResult":
    """Clone a stored result for a cache hit.

    Chunk 5 / Trust Upgrade: stamps ``cache_entry_created_ts`` from the
    sidecar ``_CacheEntry.created_ts`` so the report can render the cache-age
    badge ("Cache replay — Nd old") without re-reading the cache file. The
    age field lives on ``VerificationResult`` as runtime telemetry —
    distinct from the entry creation timestamp which remains the cache's
    source of truth.
    """
    return _result_from_dict(
        _result_to_dict(entry.result),
        cache_status="hit",
        cache_entry_created_ts=entry.created_ts,
    )
