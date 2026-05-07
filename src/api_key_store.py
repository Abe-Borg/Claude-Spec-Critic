"""Loading the Anthropic API key from file-based configuration.

The key is searched for in the platform config directory first, then in the
executable/source-parent fallback. Returns an empty string for any
missing/unreadable file so the caller can decide how to surface that to
the user.
"""
from __future__ import annotations

from .app_paths import api_key_paths


def load_api_key_from_file() -> str:
    for path in api_key_paths():
        if not path.exists():
            continue
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
    return ""
