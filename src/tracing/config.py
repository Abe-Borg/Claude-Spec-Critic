"""Tracing configuration: env-var parsing, capture-level enum, trace dir.

Boolean env vars use the same disable-token convention as the rest of the
codebase (``0`` / ``false`` / ``no`` / ``off``, case-insensitive) so an
operator who already knows how to disable other Spec Critic flags can
disable tracing without consulting the docs.

Default-on for the main trace flag; default-off for deep mode. Deep mode
implies trace enabled.
"""
from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_state_dir


# Capture levels. Strings so they serialize directly into run.json without
# converting from an enum class.
LEVEL_OFF = "off"
LEVEL_DEFAULT = "default"
LEVEL_DEEP = "deep"


# Env var names — exposed as module constants so callers (and tests) can
# patch them by reference rather than hardcoding the string each time.
ENV_TRACE = "SPEC_CRITIC_TRACE"
ENV_TRACE_DEEP = "SPEC_CRITIC_TRACE_DEEP"
ENV_TRACE_DIR = "SPEC_CRITIC_TRACE_DIR"


# Canonical "disable" tokens. Mirrored from verification_cache._DISABLE_TOKENS
# — the two files are independent so no cross-module import; keep them in
# sync if either is changed.
_DISABLE_TOKENS = frozenset({"0", "false", "no", "off"})


def _env_flag_disabled(name: str) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() in _DISABLE_TOKENS


def _env_flag_enabled(name: str) -> bool:
    """True when the env var is set to a non-disable value (anything truthy).

    Distinct from ``_env_flag_disabled``: this is for default-OFF flags
    that need an explicit opt-in. Unset → False.
    """
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() not in _DISABLE_TOKENS and raw.strip() != ""


def trace_enabled() -> bool:
    """Default ON. Disable with SPEC_CRITIC_TRACE=0/false/no/off.

    Deep mode implies trace enabled even if the main flag is set to
    disable — the deep flag is a stronger signal of operator intent.
    """
    if trace_deep_enabled():
        return True
    return not _env_flag_disabled(ENV_TRACE)


def trace_deep_enabled() -> bool:
    """Default OFF. Enable with SPEC_CRITIC_TRACE_DEEP=1 (or any truthy)."""
    return _env_flag_enabled(ENV_TRACE_DEEP)


def current_capture_level() -> str:
    if not trace_enabled():
        return LEVEL_OFF
    if trace_deep_enabled():
        return LEVEL_DEEP
    return LEVEL_DEFAULT


def default_trace_root() -> Path:
    """``~/.spec_critic/traces/`` (override via ``SPEC_CRITIC_TRACE_DIR``).

    Resolves ``~`` and ``$VAR`` in the override. Does NOT create the
    directory — the recorder creates per-run subdirectories on start so
    a disabled run never touches the disk.
    """
    override = os.environ.get(ENV_TRACE_DIR)
    if override:
        return Path(os.path.expanduser(os.path.expandvars(override)))
    # Match the convention used elsewhere in the app: state dir, not
    # config dir, since traces are runtime artifacts.
    return Path(user_state_dir("SpecCritic", appauthor=False)) / "traces"


def trace_dir_for_run(run_id: str) -> Path:
    return default_trace_root() / run_id
