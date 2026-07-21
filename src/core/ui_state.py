"""Persisted GUI selections (currently: the selected review module).

A tiny JSON sidecar next to the other Spec Critic state
(``~/.spec_critic/``, matching the verification cache and the pending-batch
file). Load and save are defensive on every axis — a missing file, malformed
JSON, or an I/O error reads as "no saved selection" / silently skips the
save, never an exception into GUI startup. The stored module id is resolved
through ``modules.get_module`` at use, so a stale id from an uninstalled
module degrades to the default module rather than erroring.

Overridable via ``SPEC_CRITIC_UI_STATE_PATH`` (``~`` and ``$VAR`` expanded)
so tests never touch the real home directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .api_config import (
    REALTIME_REVIEW_MAX_WORKERS_DEFAULT,
    REALTIME_REVIEW_WORKER_CHOICES,
    realtime_review_max_workers,
)

_MODULE_KEY = "module_id"
_PROGRAM_KEY = "program_id"
# Per-module last-entered project profile, keyed by module id. A nested map so
# switching modules restores the profile last used for THAT module rather than
# carrying one domain's city/client into another.
_PROFILES_KEY = "project_profiles"
# Review transport preference: "batch" (default) or "realtime". Anything
# else — including a hand-edited file — reads as "batch", the safe default
# (50% cheaper, resumable).
_TRANSPORT_KEY = "review_transport"
_VALID_TRANSPORTS = ("batch", "realtime")
# Real-time spec-review concurrency. The GUI deliberately offers a small,
# explicit set rather than accepting arbitrary text; headless callers retain
# the wider 1-8 environment-variable surface in ``api_config``.
_REALTIME_REVIEW_WORKERS_KEY = "realtime_review_workers"
# One-time "Don't show this again" acknowledgement for the real-time cost
# warning popup. Absent/unset reads as False (show the warning), so a fresh
# install always warns on the first switch into real-time mode.
_SUPPRESS_REALTIME_COST_WARNING_KEY = "suppress_realtime_cost_warning"
# Whether the developer/diagnostic agent-tracing controls are revealed in the
# GUI. Default False — regular users don't see the tracing row; an operator
# opts in via the Options toggle. Anything non-bool (missing / hand-edited)
# reads as False.
_SHOW_TRACING_KEY = "show_tracing_tools"


def ui_state_path() -> Path:
    override = os.environ.get("SPEC_CRITIC_UI_STATE_PATH")
    if override:
        return Path(os.path.expanduser(os.path.expandvars(override)))
    return Path.home() / ".spec_critic" / "ui_state.json"


def _load(path: Path | None = None) -> dict:
    target = path or ui_state_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_selected_module_id(*, path: Path | None = None) -> str:
    """Last-selected module id, or ``""`` when none was saved / unreadable."""
    value = _load(path).get(_MODULE_KEY, "")
    return value if isinstance(value, str) else ""


def save_selected_module_id(module_id: str, *, path: Path | None = None) -> None:
    """Persist the selected module id. Best-effort: never raises."""
    _write_key(_MODULE_KEY, module_id, path=path)


def load_selected_program_id(*, path: Path | None = None) -> str:
    """Last-selected program id, falling back to the legacy module id."""
    state = _load(path)
    value = state.get(_PROGRAM_KEY, "")
    if isinstance(value, str) and value.strip():
        return value
    legacy = state.get(_MODULE_KEY, "")
    return legacy if isinstance(legacy, str) else ""


def save_selected_program_id(program_id: str, *, path: Path | None = None) -> None:
    """Persist the user-facing program without deleting legacy state keys."""
    _write_key(_PROGRAM_KEY, program_id, path=path)


def load_review_transport(*, path: Path | None = None) -> str:
    """Last-selected review transport; ``"batch"`` when unset or unknown."""
    value = _load(path).get(_TRANSPORT_KEY, "")
    return value if value in _VALID_TRANSPORTS else "batch"


def save_review_transport(transport: str, *, path: Path | None = None) -> None:
    """Persist the review transport. Best-effort: never raises.

    Unknown values are dropped rather than written, so the stored state can
    only ever hold a transport the app knows how to run.
    """
    if transport not in _VALID_TRANSPORTS:
        return
    _write_key(_TRANSPORT_KEY, transport, path=path)


def load_realtime_review_workers(*, path: Path | None = None) -> int:
    """Persisted GUI worker choice, with a backward-compatible first-run seed.

    Before the GUI selector existed, operators could configure realtime
    concurrency only through the environment. If no GUI key has ever been
    written, preserve a 2/4/6/8 environment choice; unsupported odd values
    remain valid for headless callers but degrade to the GUI default of 4.
    Once saved, the explicit GUI preference wins over the environment.
    """

    state = _load(path)
    if _REALTIME_REVIEW_WORKERS_KEY not in state:
        configured = realtime_review_max_workers()
        return (
            configured
            if configured in REALTIME_REVIEW_WORKER_CHOICES
            else REALTIME_REVIEW_MAX_WORKERS_DEFAULT
        )
    value = state.get(_REALTIME_REVIEW_WORKERS_KEY)
    if isinstance(value, bool) or value not in REALTIME_REVIEW_WORKER_CHOICES:
        return REALTIME_REVIEW_MAX_WORKERS_DEFAULT
    return int(value)


def save_realtime_review_workers(
    workers: int, *, path: Path | None = None
) -> None:
    """Persist one of the GUI's supported 2/4/6/8 worker choices."""

    if isinstance(workers, bool) or workers not in REALTIME_REVIEW_WORKER_CHOICES:
        return
    _write_key(_REALTIME_REVIEW_WORKERS_KEY, int(workers), path=path)


def load_suppress_realtime_cost_warning(*, path: Path | None = None) -> bool:
    """Whether the user asked not to see the real-time cost warning again.

    Defaults to ``False`` (show the warning) when unset or unreadable.
    """
    return bool(_load(path).get(_SUPPRESS_REALTIME_COST_WARNING_KEY, False))


def save_suppress_realtime_cost_warning(
    value: bool, *, path: Path | None = None
) -> None:
    """Persist the real-time cost-warning suppression flag. Never raises."""
    _write_key(_SUPPRESS_REALTIME_COST_WARNING_KEY, bool(value), path=path)


def load_show_tracing_tools(*, path: Path | None = None) -> bool:
    """Whether the agent-tracing controls are revealed; ``False`` by default.

    Any non-bool stored value (missing key or a hand-edited file) reads as
    ``False`` so the tracing row stays hidden unless explicitly enabled.
    """
    value = _load(path).get(_SHOW_TRACING_KEY, False)
    return value if isinstance(value, bool) else False


def save_show_tracing_tools(value: bool, *, path: Path | None = None) -> None:
    """Persist the tracing-tools reveal toggle. Best-effort: never raises."""
    _write_key(_SHOW_TRACING_KEY, bool(value), path=path)


def load_project_profile(module_id: str, *, path: Path | None = None) -> dict:
    """Last-entered project profile for ``module_id`` (``{}`` when none saved)."""
    profiles = _load(path).get(_PROFILES_KEY, {})
    if not isinstance(profiles, dict):
        return {}
    entry = profiles.get(module_id, {})
    return entry if isinstance(entry, dict) else {}


def save_project_profile(
    module_id: str, profile: dict, *, path: Path | None = None
) -> None:
    """Persist the project profile for ``module_id``. Best-effort: never raises.

    Read-modify-write so the top-level ``module_id`` selection and other
    modules' saved profiles are never clobbered.
    """
    target = path or ui_state_path()
    state = _load(target)
    profiles = state.get(_PROFILES_KEY)
    if not isinstance(profiles, dict):
        profiles = {}
    profiles[module_id] = dict(profile)
    state[_PROFILES_KEY] = profiles
    _write_state(state, path=target)


def _write_key(key: str, value: object, *, path: Path | None = None) -> None:
    target = path or ui_state_path()
    state = _load(target)
    state[key] = value
    _write_state(state, path=target)


def _write_state(state: dict, *, path: Path | None = None) -> None:
    target = path or ui_state_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        pass
