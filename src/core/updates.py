"""Self-update check + installer download for the Windows desktop build.

Spec Critic ships as a downloadable Windows app (see ``packaging/windows/``
and ``docs/RELEASE_WINDOWS.md``). There is no server: the app runs entirely on
the user's machine and talks only to the Anthropic API with the user's own key.
Updates ride on **GitHub Releases** — the same free hosting that serves the
installer:

    1. Each published release carries a tiny ``latest.json`` manifest and the
       installer (``SpecCriticSetup.exe``) as release assets.
    2. The installed app fetches ``latest.json`` (throttled to once a day on
       launch, or on demand via the "Check for Updates" button), compares the
       manifest version against its own ``__version__``, and — if newer —
       offers to download and run the new installer.
    3. The download is integrity-checked against the manifest's ``sha256``
       **before** it is ever launched. This is the free, local "signing" that
       matters: the app is distributed unsigned (no paid OS code-signing
       certificate), so Windows SmartScreen warns on the installer, but a
       tampered download can never be executed because its hash won't match.

Design constraints that mirror the rest of the codebase:

- **Pure and self-contained.** This module imports only the standard library.
  It does NOT import the heavy parent package, ``customtkinter``, or the
  Anthropic SDK, so it stays trivially unit-testable and import-cheap. The
  *caller* supplies the current version string — the update logic never
  reaches into the package to find it.
- **Non-fatal.** :func:`check_for_update` never raises: any network / parse /
  comparison failure is folded into an ``ERROR`` result the GUI can quietly
  ignore (silent auto-check) or surface (explicit "Check for Updates" click).
- **Injectable seams for hermetic tests.** The network fetcher, the download
  opener, and the clock (``now=``) are all parameters, so tests drive the
  whole flow without touching the network.

Env overrides (matching the ``SPEC_CRITIC_*`` convention in ``api_config``):

    SPEC_CRITIC_UPDATE_URL            — override the manifest URL (testing /
                                        self-hosting a fork's releases).
    SPEC_CRITIC_DISABLE_UPDATE_CHECK  — set truthy to turn the check off
                                        entirely (locked-down deployments).
    SPEC_CRITIC_UPDATE_STATE_PATH     — override the throttle-state file
                                        (``~`` and ``$VAR`` are expanded), so
                                        tests never touch the real home dir.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

__all__ = [
    "UpdateError",
    "UpdateInfo",
    "UpdateCheckResult",
    "STATUS_UP_TO_DATE",
    "STATUS_UPDATE_AVAILABLE",
    "STATUS_DISABLED",
    "STATUS_ERROR",
    "GITHUB_OWNER",
    "GITHUB_REPO",
    "parse_version",
    "is_newer",
    "manifest_url",
    "releases_page_url",
    "update_check_disabled",
    "parse_manifest",
    "fetch_manifest",
    "check_for_update",
    "verify_sha256",
    "download_installer",
    "spawn_installer",
    "default_state_path",
    "default_download_dir",
    "load_state",
    "save_state",
    "should_auto_check",
    "record_check",
    "version_is_skipped",
    "mark_skipped",
]

# --------------------------------------------------------------------------
# Release coordinates. Change these once if the repo ever moves; the manifest
# and releases-page URLs are derived from them so there is a single source of
# truth for "where do updates come from".
# --------------------------------------------------------------------------

GITHUB_OWNER = "Abe-Borg"
GITHUB_REPO = "Claude-Spec-Critic"

# GitHub serves the newest *published, non-prerelease* release's assets from the
# stable ``releases/latest/download/<asset>`` path, so this URL always points at
# the current version's manifest without the app knowing the version number.
_DEFAULT_MANIFEST_URL = (
    f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest/download/latest.json"
)
_RELEASES_PAGE_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

ENV_UPDATE_URL = "SPEC_CRITIC_UPDATE_URL"
ENV_DISABLE = "SPEC_CRITIC_DISABLE_UPDATE_CHECK"
ENV_STATE_PATH = "SPEC_CRITIC_UPDATE_STATE_PATH"

# Mirrors the disable-token convention used across the codebase (api_config,
# tracing, ui_state): any of these, case-insensitively, reads as "off".
_DISABLE_TOKENS = frozenset({"0", "false", "no", "off"})

# Network guards. The manifest is a few hundred bytes; cap the read so a
# misconfigured / hijacked URL can't stream an unbounded body into memory.
DEFAULT_MANIFEST_TIMEOUT = 8.0        # seconds — one quick GET on launch
DEFAULT_DOWNLOAD_TIMEOUT = 60.0       # seconds — per socket op during download
MAX_MANIFEST_BYTES = 64 * 1024        # 64 KiB is enormous for this manifest
_USER_AGENT = "SpecCritic-Updater"

STATUS_UP_TO_DATE = "UP_TO_DATE"
STATUS_UPDATE_AVAILABLE = "UPDATE_AVAILABLE"
STATUS_DISABLED = "DISABLED"
STATUS_ERROR = "ERROR"

STATE_FILENAME = "update_check.json"
DEFAULT_MIN_INTERVAL_DAYS = 1

# The version grammar the release-metadata test pins (tests/test_release_metadata.py):
# MAJOR.MINOR.PATCH with an optional release-candidate suffix.
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_INSTALLER_FALLBACK_NAME = "SpecCriticSetup.exe"


class UpdateError(Exception):
    """A recoverable update problem (bad manifest, checksum mismatch, ...)."""


@dataclass(frozen=True)
class UpdateInfo:
    """A validated update descriptor parsed from ``latest.json``."""

    version: str
    url: str
    sha256: str
    notes: str = ""
    published_at: str = ""


@dataclass(frozen=True)
class UpdateCheckResult:
    """Outcome of a single update check. :func:`check_for_update` never raises."""

    status: str
    current: str
    info: UpdateInfo | None = None
    error: str | None = None

    @property
    def update_available(self) -> bool:
        return self.status == STATUS_UPDATE_AVAILABLE and self.info is not None


# --------------------------------------------------------------------------
# Version comparison
# --------------------------------------------------------------------------


def parse_version(value: str) -> tuple[int, int, int, tuple[int, int]]:
    """Parse ``MAJOR.MINOR.PATCH[rcN]`` into a sortable key.

    A final release sorts *after* every release candidate of the same
    ``x.y.z`` (``1.0.0`` > ``1.0.0rc2`` > ``1.0.0rc1``). Encoded by giving a
    release candidate the pre-release rank ``(0, N)`` and a final release the
    rank ``(1, 0)`` — so tuple comparison does the right thing.

    Raises :class:`ValueError` on any string outside the pinned grammar, so a
    garbage manifest version can never masquerade as "newer".
    """
    match = _VERSION_RE.match(value.strip())
    if not match:
        raise ValueError(f"unrecognized version string: {value!r}")
    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
    rc = match.group(4)
    pre = (0, int(rc)) if rc is not None else (1, 0)
    return (major, minor, patch, pre)


def is_newer(candidate: str, current: str) -> bool:
    """Whether ``candidate`` is a strictly newer version than ``current``."""
    return parse_version(candidate) > parse_version(current)


# --------------------------------------------------------------------------
# Manifest URL / disable policy
# --------------------------------------------------------------------------


def manifest_url() -> str:
    """The manifest URL: the ``SPEC_CRITIC_UPDATE_URL`` override or default."""
    override = os.environ.get(ENV_UPDATE_URL)
    if override and override.strip():
        return override.strip()
    return _DEFAULT_MANIFEST_URL


def releases_page_url() -> str:
    """Human-facing releases page (the manual-download fallback)."""
    return _RELEASES_PAGE_URL


def update_check_disabled() -> bool:
    """Whether update checks are turned off via ``SPEC_CRITIC_DISABLE_UPDATE_CHECK``."""
    raw = os.environ.get(ENV_DISABLE)
    if raw is None:
        return False
    val = raw.strip().lower()
    return val != "" and val not in _DISABLE_TOKENS


# --------------------------------------------------------------------------
# Manifest parsing / fetching
# --------------------------------------------------------------------------


def parse_manifest(payload: dict) -> UpdateInfo:
    """Validate a decoded ``latest.json`` payload into an :class:`UpdateInfo`.

    Enforces the security-relevant invariants:

    - ``version`` must match the pinned grammar (so :func:`is_newer` can never
      raise on it).
    - ``url`` must be ``https://`` — the installer is never fetched over
      plaintext, where it could be tampered with in flight.
    - ``sha256`` must be 64 hex chars — the integrity gate that authenticates
      the downloaded binary before it is executed.
    """
    if not isinstance(payload, dict):
        raise UpdateError("manifest is not a JSON object")

    version = str(payload.get("version", "")).strip()
    if not version:
        raise UpdateError("manifest is missing 'version'")
    try:
        parse_version(version)
    except ValueError as exc:
        raise UpdateError(f"manifest version is malformed: {exc}") from exc

    url = str(payload.get("url", "")).strip()
    if not url:
        raise UpdateError("manifest is missing 'url'")
    if not url.lower().startswith("https://"):
        raise UpdateError("manifest 'url' must be https")

    sha256 = str(payload.get("sha256", "")).strip().lower()
    if not _SHA256_RE.match(sha256):
        raise UpdateError("manifest 'sha256' must be 64 hex characters")

    notes = str(payload.get("notes", "") or "")
    published_at = str(payload.get("published_at", "") or "")
    return UpdateInfo(
        version=version,
        url=url,
        sha256=sha256,
        notes=notes,
        published_at=published_at,
    )


def fetch_manifest(url: str, *, timeout: float = DEFAULT_MANIFEST_TIMEOUT) -> dict:
    """GET ``url`` and decode it as JSON, with a size cap and a short timeout.

    Uses :mod:`urllib` (standard library — no extra dependency) which honours
    the system / env proxy settings a corporate Windows box may impose.
    """
    if not url.lower().startswith("https://"):
        # The manifest is the ROOT of trust: the installer's authenticating
        # sha256 comes FROM it, so it must arrive over an authenticated channel
        # or an on-path attacker could supply both a malicious installer URL and
        # a matching hash. Enforced here (symmetric with parse_manifest's check
        # on the installer URL) so a SPEC_CRITIC_UPDATE_URL override can never
        # downgrade the manifest fetch to http.
        raise UpdateError("refusing to fetch the update manifest over a non-https URL")
    request = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - https enforced above
        raw = resp.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise UpdateError("update manifest is unexpectedly large; refusing to parse")
    return json.loads(raw.decode("utf-8"))


def check_for_update(
    current: str,
    *,
    url: str | None = None,
    fetcher: Callable[..., dict] | None = None,
    timeout: float = DEFAULT_MANIFEST_TIMEOUT,
) -> UpdateCheckResult:
    """Fetch the manifest and compare it to ``current``. Never raises.

    Returns a :class:`UpdateCheckResult`. ``fetcher`` defaults to
    :func:`fetch_manifest`; tests inject a fake to stay hermetic.
    """
    if update_check_disabled():
        return UpdateCheckResult(status=STATUS_DISABLED, current=current)

    fetch = fetcher or fetch_manifest
    target = url or manifest_url()
    try:
        payload = fetch(target, timeout=timeout)
        info = parse_manifest(payload)
        newer = is_newer(info.version, current)
    except Exception as exc:  # noqa: BLE001 - the check is best-effort, never fatal
        return UpdateCheckResult(status=STATUS_ERROR, current=current, error=str(exc))

    if newer:
        return UpdateCheckResult(
            status=STATUS_UPDATE_AVAILABLE, current=current, info=info
        )
    return UpdateCheckResult(status=STATUS_UP_TO_DATE, current=current, info=info)


# --------------------------------------------------------------------------
# Download + integrity + launch
# --------------------------------------------------------------------------


def verify_sha256(path: str | Path, expected: str, *, chunk: int = 1 << 20) -> str:
    """Return the SHA-256 hex digest of ``path`` and confirm it matches ``expected``.

    Raises :class:`UpdateError` on mismatch. Streams the file so a large
    installer never has to sit in memory.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    actual = digest.hexdigest()
    if actual.lower() != expected.strip().lower():
        raise UpdateError(
            f"downloaded installer failed integrity check "
            f"(expected {expected}, got {actual})"
        )
    return actual


def _installer_filename(url: str) -> str:
    """A safe, basename-only filename derived from the download URL.

    Guards against path traversal (``..``) and directory separators sneaking in
    from a manifest URL — the downloaded file always lands directly in the
    destination directory with a ``.exe`` name.
    """
    tail = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    name = os.path.basename(tail)
    if not name or not name.lower().endswith(".exe"):
        return _INSTALLER_FALLBACK_NAME
    return name


def _open_url(url: str, *, timeout: float):
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - https enforced by caller


def download_installer(
    info: UpdateInfo,
    dest_dir: str | Path,
    *,
    opener: Callable[..., object] | None = None,
    progress: Callable[[int, int], None] | None = None,
    timeout: float = DEFAULT_DOWNLOAD_TIMEOUT,
    chunk: int = 1 << 16,
) -> Path:
    """Download ``info.url`` into ``dest_dir`` and verify its SHA-256.

    Returns the path to the verified installer. The download streams to a
    ``.part`` temp file and is promoted to the final name with an atomic
    ``os.replace`` **only** after the SHA-256 matches — so the installer path
    never holds a partial or failed file. Any failure (non-https URL,
    interrupted transfer, disk error, checksum mismatch) raises
    :class:`UpdateError` (or re-raises the transfer error) and removes the temp
    file, so a bad download is never left behind to be run by mistake.
    ``opener`` and ``progress`` are seams for tests / the GUI's progress bar;
    ``opener(url, timeout=...)`` must return a context-managed response exposing
    ``.read(n)`` (and optionally a ``Content-Length`` header).
    """
    if not info.url.lower().startswith("https://"):
        raise UpdateError("refusing to download an installer over a non-https URL")

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _installer_filename(info.url)
    part = dest.with_name(dest.name + ".part")

    open_fn = opener or _open_url
    digest = hashlib.sha256()
    downloaded = 0
    try:
        with open_fn(info.url, timeout=timeout) as resp:
            total = _content_length(resp)
            with open(part, "wb") as fh:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    fh.write(buf)
                    digest.update(buf)
                    downloaded += len(buf)
                    if progress is not None:
                        progress(downloaded, total)
        actual = digest.hexdigest()
        if actual.lower() != info.sha256.lower():
            raise UpdateError(
                f"downloaded installer failed integrity check "
                f"(expected {info.sha256}, got {actual})"
            )
        # Promote atomically: the final installer path only ever appears as a
        # fully-downloaded, integrity-verified file.
        os.replace(part, dest)
    except BaseException:
        # Interrupted transfer, disk error, or checksum mismatch — remove the
        # temp file so nothing partial survives for a user to run by mistake.
        try:
            part.unlink()
        except OSError:
            pass
        raise
    return dest


def _content_length(resp) -> int:
    """Best-effort ``Content-Length`` for progress reporting (0 when unknown)."""
    getter = getattr(resp, "getheader", None)
    raw = None
    if callable(getter):
        raw = getter("Content-Length")
    if raw is None:
        headers = getattr(resp, "headers", None)
        if headers is not None and hasattr(headers, "get"):
            raw = headers.get("Content-Length")
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def spawn_installer(path: str | Path) -> None:
    """Launch the downloaded installer and return so the app can exit.

    On Windows the installer runs detached; the caller then quits the app so
    the (Inno Setup) installer can replace the running files and relaunch. On
    other platforms this best-effort ``Popen`` exists for parity / testing —
    the shipped product is Windows-only.
    """
    path = Path(path)
    if sys.platform.startswith("win"):
        # os.startfile launches via the shell association and returns
        # immediately, fully detached from this process.
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606 - Windows-only launch of a verified file
    else:  # pragma: no cover - the product is Windows-only
        subprocess.Popen([str(path)])  # noqa: S603


# --------------------------------------------------------------------------
# Throttle state (once-a-day auto-check + "skip this version")
# --------------------------------------------------------------------------


def _state_dir() -> Path:
    """The ``~/.spec_critic`` dot-dir the app's other state files live in."""
    return Path.home() / ".spec_critic"


def default_state_path() -> Path:
    """Where the last-check timestamp / skipped-version marker is persisted.

    ``SPEC_CRITIC_UPDATE_STATE_PATH`` overrides the location (``~`` and
    ``$VAR`` expanded) — the same test seam every other ``~/.spec_critic``
    state file exposes (ui_state, pending batch, verification cache).
    """
    override = os.environ.get(ENV_STATE_PATH)
    if override and override.strip():
        return Path(os.path.expanduser(os.path.expandvars(override.strip())))
    return _state_dir() / STATE_FILENAME


def default_download_dir() -> Path:
    """Where downloaded installers land (``~/.spec_critic/updates``)."""
    return _state_dir() / "updates"


def load_state(path: str | Path) -> dict:
    """Load the persisted check state, tolerating a missing / corrupt file."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: str | Path, state: dict) -> None:
    """Persist the check state (best-effort; a write failure is non-fatal)."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def should_auto_check(
    state: dict,
    *,
    now: datetime,
    min_interval_days: int = DEFAULT_MIN_INTERVAL_DAYS,
) -> bool:
    """Whether enough time has elapsed since the last automatic check.

    ``now`` is injected so the once-a-day throttle is deterministic under
    test. A missing or unparseable ``last_check`` means "never checked" →
    check now.
    """
    last = state.get("last_check")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last))
    except ValueError:
        return True
    return (now - last_dt) >= timedelta(days=min_interval_days)


def record_check(state: dict, *, now: datetime) -> dict:
    """Stamp ``state`` with the current check time. Mutates and returns it."""
    state["last_check"] = now.isoformat()
    return state


def version_is_skipped(state: dict, version: str) -> bool:
    """Whether the user chose "Skip this version" for ``version``."""
    return state.get("skipped_version") == version


def mark_skipped(state: dict, version: str) -> dict:
    """Record that the user wants no more prompts for ``version``."""
    state["skipped_version"] = version
    return state
