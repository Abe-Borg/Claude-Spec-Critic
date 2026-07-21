"""Phase 9 (plan section 13.2) — extraction and token-count cache.

Spec extraction and ``cl100k_base`` token counts are deterministic from a
file's bytes. Re-running a review after toggling UI options or selecting the
same project a second time should not re-parse unchanged DOCX files. This
module provides a small, in-process LRU keyed on file identity and an
opt-in token-count cache keyed on a content + configuration hash.

The cache is intentionally process-local and bounded — DOCX extraction
already completes in milliseconds, but the savings add up across a long
session (e.g. resubmitting a batch after a parameter tweak).

Design notes:
    * The extraction key is ``(absolute_path, size, mtime_ns)``. We hash file
      bytes only when the cheap stat-based key is ambiguous (mtime collision
      across writes within the same nanosecond — rare but easy to defeat by
      checking content hash).
    * The cache is not persisted to disk. Crash recovery is handled by the
      resume-state subsystem; mixing the two would force a sensitive-data
      retention decision (Phase 6).
    * ``ExtractedSpec`` instances are mutable, so we deep-copy on hit to
      prevent a caller mutation (e.g. setting ``paragraph_map`` to ``None``
      for a derived view) from leaking into the next consumer.
"""
from __future__ import annotations

import copy
import hashlib
import threading
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from .extractor import ExtractedSpec


_DEFAULT_MAX_ENTRIES = 64
_DEFAULT_TOKEN_MAX_ENTRIES = 256

# Byte length of head+tail samples folded into the cache fingerprint. Two
# 64-KiB reads are cheap (single OS read each on typical SSDs) and catch the
# realistic ways the stat-based key can lie:
#   * `touch -d` style mtime preservation across a content edit
#   * Same-size in-place edits (e.g. cosmetic whitespace replacements that
#     keep file length identical)
#   * Atomic rename-over with a copy that preserves both size and mtime_ns
# A DOCX file's central directory and a few opening XML parts both land near
# the head/tail, so 64 KiB on each end is sufficient to detect any practical
# bit-level change without paying for a full-file SHA on every cache lookup.
_FINGERPRINT_SAMPLE_BYTES = 64 * 1024


def _content_fingerprint(path: Path, size: int) -> str:
    """Cheap content fingerprint: SHA-256 of size + head + tail bytes.

    This guards against the same-size+same-mtime collision case that the
    stat-only key cannot detect. Reading at most ~128 KiB per file keeps the
    overhead well under a single DOCX parse, so the cache still pays for
    itself on a typical 200-file run.

    Failures (transient I/O error, file disappeared between stat and open)
    return an empty string; the caller treats that as "cannot fingerprint"
    and falls back to the stat-only key.
    """
    if size <= 0:
        return hashlib.sha256(b"empty").hexdigest()
    h = hashlib.sha256()
    h.update(str(size).encode("ascii"))
    h.update(b"\x00")
    try:
        with open(path, "rb") as fp:
            head = fp.read(_FINGERPRINT_SAMPLE_BYTES)
            h.update(head)
            if size > _FINGERPRINT_SAMPLE_BYTES:
                tail_start = max(_FINGERPRINT_SAMPLE_BYTES, size - _FINGERPRINT_SAMPLE_BYTES)
                fp.seek(tail_start)
                h.update(fp.read(_FINGERPRINT_SAMPLE_BYTES))
    except OSError:
        return ""
    return h.hexdigest()


class _ExtractionCache:
    """Thread-safe bounded cache for ExtractedSpec objects.

    Lookup key: ``(absolute_path, size, mtime_ns, content_fingerprint)``.
    Adding the head+tail fingerprint catches the case where an edit
    preserves both size and mtime_ns (e.g. ``touch -d`` after a same-size
    in-place tweak), which the prior stat-only key would have missed and
    returned stale extraction data for. Eviction policy is
    least-recently-used. On a hit, a deep copy is returned so callers cannot
    accidentally mutate cached state.
    """

    def __init__(self, max_entries: int = _DEFAULT_MAX_ENTRIES) -> None:
        self._max_entries = int(max_entries)
        self._entries: "OrderedDict[tuple, ExtractedSpec]" = OrderedDict()
        # A cold path may be requested concurrently by several routed-module
        # preparation workers.  The cache lock protected the mapping itself,
        # but the former get-then-extract sequence allowed every caller to
        # observe the same miss and parse the DOCX independently.  One Future
        # per cache key makes that work single-flight without holding the lock
        # during extraction.
        self._inflight: dict[tuple, Future[ExtractedSpec]] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _key(path: Path) -> tuple[str, int, int, str]:
        st = path.stat()
        resolved = str(path.resolve())
        fingerprint = _content_fingerprint(path, st.st_size)
        return (resolved, st.st_size, st.st_mtime_ns, fingerprint)

    def get(self, path: Path) -> Optional[ExtractedSpec]:
        try:
            key = self._key(path)
        except FileNotFoundError:
            return None
        with self._lock:
            spec = self._entries.get(key)
            if spec is None:
                self._misses += 1
                return None
            # Refresh LRU position.
            self._entries.move_to_end(key)
            self._hits += 1
            return copy.deepcopy(spec)

    def put(self, path: Path, spec: ExtractedSpec) -> None:
        try:
            key = self._key(path)
        except FileNotFoundError:
            return
        with self._lock:
            self._entries[key] = copy.deepcopy(spec)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def _lookup_or_reserve(
        self, path: Path
    ) -> tuple[str, Optional[ExtractedSpec], tuple | None, Future[ExtractedSpec] | None]:
        """Return a cache hit or reserve/join one extraction for ``path``.

        The status is one of ``hit``, ``leader``, ``waiter``, or
        ``uncached``.  ``uncached`` preserves the historical missing-file
        behavior: the downstream extractor owns the error/result because no
        stable file-identity key exists to coordinate on.
        """

        try:
            key = self._key(path)
        except FileNotFoundError:
            return "uncached", None, None, None
        with self._lock:
            spec = self._entries.get(key)
            if spec is not None:
                self._entries.move_to_end(key)
                self._hits += 1
                return "hit", copy.deepcopy(spec), key, None

            # Preserve the old stats contract: every caller that arrives
            # before the value is cached observes a miss, including waiters.
            self._misses += 1
            future = self._inflight.get(key)
            if future is not None:
                return "waiter", None, key, future
            future = Future()
            self._inflight[key] = future
            return "leader", None, key, future

    def _complete_reservation(
        self,
        path: Path,
        key: tuple,
        future: Future[ExtractedSpec],
        spec: ExtractedSpec,
    ) -> None:
        """Publish a leader result to the cache and every waiting caller."""

        try:
            # ``put`` preserves the existing post-extraction identity check:
            # if the file changed while it was parsed, the cached value is
            # stored under the new identity rather than the stale claim key.
            self.put(path, spec)
            waiter_copy = copy.deepcopy(spec)
        except BaseException as exc:
            self._fail_reservation(key, future, exc)
            raise
        with self._lock:
            if self._inflight.get(key) is future:
                del self._inflight[key]
            if not future.done():
                # Waiters deepcopy this snapshot once more, so no two callers
                # ever share a mutable ExtractedSpec or nested warning/map list.
                future.set_result(waiter_copy)

    def _fail_reservation(
        self,
        key: tuple,
        future: Future[ExtractedSpec],
        exc: BaseException,
    ) -> None:
        """Release every waiter with the leader's extraction exception."""

        with self._lock:
            if self._inflight.get(key) is future:
                del self._inflight[key]
            if not future.done():
                future.set_exception(exc)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._entries),
                "max_entries": self._max_entries,
            }


_extraction_cache = _ExtractionCache()


def cache_enabled() -> bool:
    """Whether the extraction cache is active. Always True."""
    return True


def extract_multiple_specs_cached(
    filepaths: list[Path],
    *,
    max_workers: int | None = None,
) -> list[ExtractedSpec]:
    """Cached counterpart to :func:`extract_multiple_specs`.

    Splits inputs into hits and misses, runs misses through the existing
    parallel extractor, then merges back in original order.
    """
    if not filepaths:
        return []
    paths = [Path(fp) for fp in filepaths]
    if not cache_enabled():
        from .extractor import extract_multiple_specs
        return extract_multiple_specs(paths, max_workers=max_workers)

    out: list[Optional[ExtractedSpec]] = [None] * len(paths)
    leaders: list[
        tuple[int, Path, tuple | None, Future[ExtractedSpec] | None]
    ] = []
    waiters: list[tuple[int, Future[ExtractedSpec]]] = []
    for i, p in enumerate(paths):
        status, cached, key, future = _extraction_cache._lookup_or_reserve(p)
        if status == "hit":
            out[i] = cached
        elif status == "waiter":
            assert future is not None
            waiters.append((i, future))
        else:
            # ``uncached`` has no stable key/future; it still travels through
            # the existing bulk extractor and preserves its error behavior.
            leaders.append((i, p, key, future))
    if leaders:
        from .extractor import extract_multiple_specs

        # Resolve owned reservations independently.  A bulk ``pool.map``
        # raises for the whole list when one DOCX is corrupt; forwarding that
        # one exception to every per-key Future would incorrectly make a
        # concurrent waiter for a healthy shared file fail too.  The small
        # outer pool retains parallel extraction while capturing one outcome
        # per identity.  Each one-item extractor call is sequential internally
        # (no nested worker fan-out).
        configured_workers = max_workers if max_workers is not None else min(8, len(leaders))
        workers = max(1, min(int(configured_workers), len(leaders)))

        def extract_one(path: Path) -> ExtractedSpec:
            result = extract_multiple_specs([path], max_workers=1)
            if len(result) != 1:
                raise RuntimeError(
                    "Specification extraction returned an unexpected result count "
                    f"({len(result)} for 1 input file)."
                )
            return result[0]

        outcomes: dict[int, ExtractedSpec | BaseException] = {}
        if workers == 1:
            for position, (_idx, path, _key, _future) in enumerate(leaders):
                try:
                    outcomes[position] = extract_one(path)
                except BaseException as exc:
                    outcomes[position] = exc
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(extract_one, path): position
                    for position, (_idx, path, _key, _future) in enumerate(leaders)
                }
                for completed in as_completed(futures):
                    position = futures[completed]
                    try:
                        outcomes[position] = completed.result()
                    except BaseException as exc:
                        outcomes[position] = exc

        first_error: BaseException | None = None
        for position, (idx, path, key, future) in enumerate(leaders):
            outcome = outcomes[position]
            if isinstance(outcome, BaseException):
                if key is not None and future is not None:
                    _extraction_cache._fail_reservation(key, future, outcome)
                if first_error is None:
                    first_error = outcome
                continue
            try:
                if key is not None and future is not None:
                    _extraction_cache._complete_reservation(path, key, future, outcome)
                else:
                    _extraction_cache.put(path, outcome)
                out[idx] = outcome
            except BaseException as exc:
                if key is not None and future is not None:
                    _extraction_cache._fail_reservation(key, future, exc)
                if first_error is None:
                    first_error = exc

        if first_error is not None:
            raise first_error

    for idx, future in waiters:
        # Future.result propagates the leader's exception unchanged.  Copying
        # its immutable snapshot preserves the cache's caller-isolation rule.
        out[idx] = copy.deepcopy(future.result())
    # Every slot is now populated.
    return [s for s in out if s is not None]


def extraction_cache_stats() -> dict:
    return _extraction_cache.stats()


# ---------------------------------------------------------------------------
# Token-count cache (plan 13.2: "exact token preflight is reused when prompt
# /model/config are unchanged"). Keyed on a content + config hash so callers
# do not accidentally share counts across cycles, models, or modes.
# ---------------------------------------------------------------------------


class _TokenCountCache:
    """Bounded cache for exact token counts keyed on a config digest."""

    def __init__(self, max_entries: int = _DEFAULT_TOKEN_MAX_ENTRIES) -> None:
        self._max_entries = int(max_entries)
        self._entries: "OrderedDict[str, int]" = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[int]:
        with self._lock:
            value = self._entries.get(key)
            if value is None:
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return value

    def put(self, key: str, value: int) -> None:
        with self._lock:
            self._entries[key] = int(value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._entries),
                "max_entries": self._max_entries,
            }


_token_cache = _TokenCountCache()


def token_count_cache_key(
    *,
    model: str,
    system_prompt: str,
    user_message: str,
    project_context: str = "",
    cycle_label: str = "",
    tools: Optional[list[dict]] = None,
    extra: Optional[dict] = None,
) -> str:
    """Deterministic digest of inputs that influence the API token count.

    Including ``cycle_label`` and ``tools`` prevents collisions when the
    same spec is reviewed under a different code cycle or tool definition
    (each of which materially changes the input token count).
    """
    h = hashlib.sha256()
    parts = [
        model or "",
        system_prompt or "",
        user_message or "",
        project_context or "",
        cycle_label or "",
    ]
    if tools:
        # Hash the tool list as a stable JSON serialization so any change to
        # tool definitions (schema, description, name, strict flag) busts
        # the cache.
        import json as _json
        try:
            parts.append(_json.dumps(tools, sort_keys=True, default=str))
        except Exception:
            parts.append(str(tools))
    if extra:
        for k in sorted(extra.keys()):
            parts.append(f"{k}={extra[k]}")
    for p in parts:
        h.update(p.encode("utf-8", errors="replace"))
        h.update(b"\x00")
    return h.hexdigest()


def get_cached_token_count(key: str) -> Optional[int]:
    return _token_cache.get(key)


def cache_token_count(key: str, value: int) -> None:
    _token_cache.put(key, value)


def clear_token_cache() -> None:
    _token_cache.clear()
