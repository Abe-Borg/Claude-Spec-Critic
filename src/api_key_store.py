"""Loading and storing the Anthropic API key.

The key is searched for in the platform config directory first, then in the
executable/source-parent fallback. Returns an empty string for any
missing/unreadable file so the caller can decide how to surface that to
the user.

OS keyring (optional)
---------------------
When the ``keyring`` package is installed and a working backend is available,
the keyring is consulted *first* — keychain / credential-manager / kwallet
secrets are at least as safe as a plaintext file and survive a stray
``cat`` / scp of the config directory. The plaintext file remains a
fallback so the legacy "drop a key file next to the exe" workflow keeps
working unchanged and existing users are never locked out of their saved
key when they upgrade.

File permissions
----------------
On POSIX, :func:`save_api_key_to_file` chmods the file to ``0600`` (owner
read+write only) so a freshly-written fallback never lands world-readable.
:func:`load_api_key_from_file` lazily tightens the permissions of any
fallback file it can read so an in-place upgrade improves the existing
key file's posture without requiring the user to re-enter the key.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from .app_paths import api_key_paths

try:
    import keyring as _keyring

    _KEYRING_AVAILABLE = True
except Exception:
    _keyring = None
    _KEYRING_AVAILABLE = False

_KEYRING_SERVICE = "SpecCritic"
_KEYRING_USERNAME = "anthropic_api_key"


def _keyring_get() -> str:
    if not _KEYRING_AVAILABLE or _keyring is None:
        return ""
    try:
        value = _keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
    except Exception:
        return ""
    return (value or "").strip()


def _keyring_set(value: str) -> bool:
    if not _KEYRING_AVAILABLE or _keyring is None:
        return False
    try:
        _keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, value)
    except Exception:
        return False
    return True


def _restrict_permissions(path: Path) -> None:
    """Best-effort tighten of file permissions to owner-only (0600).

    POSIX-only; on Windows ``os.chmod`` only toggles the read-only bit so
    we skip it there. Failures are swallowed because the key is still
    readable — we'd rather load the key on a quirky filesystem than fail
    the whole run over a permission tweak.
    """
    if os.name != "posix":
        return
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def load_api_key_from_file() -> str:
    """Resolve the Anthropic API key from keyring or the fallback file.

    Keyring is preferred when available; the file is searched only when the
    keyring returns nothing. Any fallback file we successfully read is
    chmod-tightened in-place so a stale 0644 key file from before this
    hardening lands at 0600 after first load.
    """
    from_keyring = _keyring_get()
    if from_keyring:
        return from_keyring
    for path in api_key_paths():
        if not path.exists():
            continue
        try:
            value = path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if value:
            _restrict_permissions(path)
            return value
    return ""


def save_api_key_to_file(value: str) -> Path | None:
    """Persist the API key to the primary fallback file with 0600 perms.

    Returns the path written, or ``None`` if ``value`` is empty. The caller
    is responsible for confirming the user intended this (the GUI prompts
    explicitly). Keyring callers should use :func:`save_api_key_to_keyring`.
    """
    value = (value or "").strip()
    if not value:
        return None
    target = api_key_paths()[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value, encoding="utf-8")
    _restrict_permissions(target)
    return target


def save_api_key_to_keyring(value: str) -> bool:
    """Persist the API key to the OS keyring when available.

    Returns ``True`` on success, ``False`` if the keyring is unavailable
    or the backend rejected the write. Callers should fall back to
    :func:`save_api_key_to_file` on ``False``.
    """
    value = (value or "").strip()
    if not value:
        return False
    return _keyring_set(value)


def keyring_available() -> bool:
    """Whether OS-keyring storage is wired up in this environment."""
    return _KEYRING_AVAILABLE
