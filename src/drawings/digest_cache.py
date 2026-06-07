"""Persistent, content-keyed cache for per-sheet drawing digests.

A digest is the dominant cost in the drawing pipeline — one Opus 4.8 vision
request per sheet (image tokens + minutes of latency). The result is
deterministic given the rendered sheet images + the model + the digest prompt +
the request params, so re-running a set (after editing one sheet, or just
re-opening the project) should not re-pay for the sheets that didn't change.

This mirrors ``verification_cache``'s persistence shape — JSON on disk, atomic
write, defensive load, env-overridable path/toggle — but is far simpler:
entries never expire (the key already invalidates on any content / model /
prompt change) and only the durable digest text + token telemetry are stored.

Thread-safe by design: ``digest_sheet`` calls may run concurrently (the parallel
dispatch follow-up), so ``get`` / ``put`` / save are guarded by a lock.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1

_FALSEY = {"0", "false", "no", "off", ""}


def _env_truthy(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in _FALSEY


def default_cache_path() -> Path:
    """On-disk digest-cache location.

    Overridable via ``SPEC_CRITIC_DRAWING_CACHE_PATH`` (``~`` and ``$VAR``
    expanded); defaults to ``~/.spec_critic/drawing_digest_cache.json``,
    alongside the verification cache.
    """
    override = os.environ.get("SPEC_CRITIC_DRAWING_CACHE_PATH")
    if override and override.strip():
        return Path(os.path.expandvars(os.path.expanduser(override.strip())))
    return Path.home() / ".spec_critic" / "drawing_digest_cache.json"


def persistence_enabled() -> bool:
    """Whether the default digest cache persists to disk (default on)."""
    return _env_truthy(os.environ.get("SPEC_CRITIC_DRAWING_CACHE_PERSIST"), default=True)


def digest_cache_key(
    sheet: Any,
    *,
    model: str,
    prompt_version: str,
    max_tokens: int,
    effort: str | None,
    use_thinking: bool,
) -> str:
    """Content-address one sheet's digest request.

    The rendered images **are** the model's input, so hashing them captures the
    page content *and* every tiling parameter at once (different rows / cols /
    overlap → different crops → different bytes → different key). Folding in the
    model, prompt fingerprint, and output-shaping params means a model swap or a
    prompt edit re-digests rather than serving a stale cached read.
    """
    h = hashlib.sha256()
    for part in (
        f"schema={_SCHEMA_VERSION}",
        f"model={model or ''}",
        f"prompt={prompt_version or ''}",
        f"max_tokens={int(max_tokens)}",
        f"effort={effort or ''}",
        f"thinking={'1' if use_thinking else '0'}",
    ):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    h.update(sheet.overview.png_bytes)
    for tile in sheet.tiles:
        h.update(tile.png_bytes)
    return h.hexdigest()


class DigestCache:
    """Thread-safe digest store, optionally persisted to ``path``.

    ``persist=False`` (or ``path=None``) keeps it purely in-memory — used by
    tests and by an explicit opt-out — so a hermetic run never touches the
    user's real cache file.
    """

    def __init__(self, path: Path | None = None, *, persist: bool = True) -> None:
        self._path = path
        self._persist = bool(persist and path is not None)
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}
        self._hits = 0
        self._misses = 0
        if self._persist:
            self._load()

    def get(self, key: str) -> dict | None:
        with self._lock:
            value = self._entries.get(key)
            if value is None:
                self._misses += 1
                return None
            self._hits += 1
            return dict(value)

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._entries[key] = dict(value)
            if self._persist:
                try:
                    self._save_locked()
                except Exception:
                    # A cache-write failure must never sink a run; the digest is
                    # already computed and returned to the caller.
                    pass

    def stats(self) -> dict:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._entries),
            }

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # missing or corrupt → start empty (never raise on load)
        if not isinstance(raw, dict) or raw.get("_schema_version") != _SCHEMA_VERSION:
            return
        entries = raw.get("entries")
        if isinstance(entries, dict):
            self._entries = {k: v for k, v in entries.items() if isinstance(v, dict)}

    def _save_locked(self) -> None:
        target = self._path
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {"_schema_version": _SCHEMA_VERSION, "entries": self._entries}
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=".drawing_digest_cache.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
                json.dump(payload, fp)
            os.replace(tmp_name, target)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


_default_cache: DigestCache | None = None
_default_lock = threading.Lock()


def get_default_digest_cache() -> DigestCache:
    """Process-wide digest cache, built once from the env config.

    Only the real run paths (the GUI / standalone analyzer, via
    ``extract_drawing_context(use_cache=True)``) reach this; unit tests inject
    their own :class:`DigestCache` so they never touch the on-disk file.
    """
    global _default_cache
    with _default_lock:
        if _default_cache is None:
            persist = persistence_enabled()
            path = default_cache_path() if persist else None
            _default_cache = DigestCache(path, persist=persist)
        return _default_cache
