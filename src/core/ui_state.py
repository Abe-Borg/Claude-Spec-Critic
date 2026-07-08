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

_MODULE_KEY = "module_id"


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
    target = path or ui_state_path()
    state = _load(target)
    state[_MODULE_KEY] = module_id
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        pass
