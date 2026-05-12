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
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from .extractor import ExtractedSpec, extract_text


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
    """Return True unless the cache is explicitly disabled.

    The cache is on by default. Set ``SPEC_CRITIC_EXTRACTION_CACHE=0`` to
    disable for debugging or reproducibility runs.
    """
    return os.environ.get("SPEC_CRITIC_EXTRACTION_CACHE", "1") != "0"


def extract_text_cached(filepath: Path) -> ExtractedSpec:
    """Return a cached ExtractedSpec when the file's identity is unchanged."""
    path = Path(filepath)
    if not cache_enabled():
        return extract_text(path)
    cached = _extraction_cache.get(path)
    if cached is not None:
        return cached
    spec = extract_text(path)
    _extraction_cache.put(path, spec)
    return spec


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
    misses: list[tuple[int, Path]] = []
    for i, p in enumerate(paths):
        cached = _extraction_cache.get(p)
        if cached is not None:
            out[i] = cached
        else:
            misses.append((i, p))
    if misses:
        from .extractor import extract_multiple_specs
        miss_paths = [p for _, p in misses]
        extracted = extract_multiple_specs(miss_paths, max_workers=max_workers)
        for (idx, path), spec in zip(misses, extracted):
            _extraction_cache.put(path, spec)
            out[idx] = spec
    # Every slot is now populated.
    return [s for s in out if s is not None]


def extraction_cache_stats() -> dict:
    return _extraction_cache.stats()


def clear_extraction_cache() -> None:
    _extraction_cache.clear()


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


def token_cache_stats() -> dict:
    return _token_cache.stats()


def clear_token_cache() -> None:
    _token_cache.clear()
