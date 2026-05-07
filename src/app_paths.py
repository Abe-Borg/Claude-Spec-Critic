"""App-specific filesystem paths and filenames.

Centralizes the locations Spec Critic uses for persistent state and config —
the API key file, batch resume state, and any other app-owned files. Path
helpers create directories on demand so callers can read/write without
their own setup boilerplate.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from platformdirs import user_config_dir, user_state_dir

API_KEY_FILENAME = "spec_critic_api_key.txt"
BATCH_STATE_FILENAME = "batch_state.json"

BATCH_STATE_MAX_AGE_HOURS = 24 * 30


def app_config_dir() -> Path:
    d = Path(user_config_dir("SpecCritic", appauthor=False))
    d.mkdir(parents=True, exist_ok=True)
    return d


def app_state_dir() -> Path:
    d = Path(user_state_dir("SpecCritic", appauthor=False))
    d.mkdir(parents=True, exist_ok=True)
    return d


def executable_dir() -> Path:
    """Directory containing the running source/executable.

    Used as the fallback location for the API key file so the legacy "drop
    a key file next to the .exe" convention keeps working alongside the
    platform config dir.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def api_key_paths() -> list[Path]:
    """Candidate locations to read the API key from, in priority order."""
    return [
        app_config_dir() / API_KEY_FILENAME,
        executable_dir() / API_KEY_FILENAME,
    ]


def batch_state_path() -> Path:
    return app_state_dir() / BATCH_STATE_FILENAME


# Backward-compatible private aliases (the legacy gui.py used underscore-prefixed
# names). Kept for any external callers; new code should use the public names.
_app_config_dir = app_config_dir
_app_state_dir = app_state_dir
