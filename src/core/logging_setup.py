"""File logging for the desktop app.

The windowed Windows build runs with ``console=False``, so ``sys.stderr`` is
``None`` and Python's ``logging.lastResort`` handler drops every record the
app emits — the capability whitelist's "unknown model id" warning, the trace
writer's crash report, ``count_tokens_via_api`` failures. Nothing in the
process ever attached a handler. :func:`configure_file_logging` attaches ONE
rotating file handler on the root logger so those records land on disk
(default ``~/.spec_critic/logs/spec_critic.log``, alongside the other
``~/.spec_critic`` state; override with ``SPEC_CRITIC_LOG_PATH``).

Contract:

* Idempotent — a second call is a no-op (the handler is tagged), so the
  GUI entry point and the frozen entry point can both call it.
* Never raises — a log directory that cannot be created (read-only
  install, locked-down profile) degrades to no file logging, never to a
  crash at startup.
* Returns the log path when a handler is active, ``None`` otherwise.
* Level defaults to ``WARNING``: the file is a place to find out why
  something silently degraded, not a trace of every call — tracing owns
  that (``SPEC_CRITIC_TRACE``).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

_HANDLER_TAG = "spec_critic_file_log"
_DEFAULT_MAX_BYTES = 2 * 1024 * 1024
_DEFAULT_BACKUPS = 3


def default_log_path() -> Path:
    override = os.environ.get("SPEC_CRITIC_LOG_PATH", "").strip()
    if override:
        return Path(os.path.expandvars(os.path.expanduser(override)))
    return Path.home() / ".spec_critic" / "logs" / "spec_critic.log"


def _existing_handler(root: logging.Logger) -> logging.Handler | None:
    for handler in root.handlers:
        if getattr(handler, _HANDLER_TAG, False):
            return handler
    return None


def configure_file_logging(
    *,
    level: int = logging.WARNING,
    path: Path | None = None,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    backup_count: int = _DEFAULT_BACKUPS,
) -> Path | None:
    """Attach the app's rotating file handler to the root logger (idempotent)."""
    root = logging.getLogger()
    existing = _existing_handler(root)
    if existing is not None:
        return Path(getattr(existing, "baseFilename", "")) or None
    target = path or default_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            target, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
    except Exception:  # noqa: BLE001 — logging must never take the app down
        return None
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    setattr(handler, _HANDLER_TAG, True)
    root.addHandler(handler)
    # The root logger defaults to WARNING already; only lower it, never raise
    # it above what a caller asked for (a test may have set DEBUG).
    if root.level > level:
        root.setLevel(level)
    return target


def remove_file_logging() -> None:
    """Detach the handler (tests; process shutdown does not need it)."""
    root = logging.getLogger()
    existing = _existing_handler(root)
    if existing is not None:
        root.removeHandler(existing)
        try:
            existing.close()
        except Exception:  # noqa: BLE001
            pass
