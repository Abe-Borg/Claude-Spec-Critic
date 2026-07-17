"""Hermetic tests for the self-update checker (``src/core/updates.py``).

No network, no clock dependence: the manifest fetcher, the download opener, and
the throttle clock (``now=``) are all injected. Covers version ordering (rc vs
final), manifest validation (the https / sha256 security gates),
``check_for_update`` never raising, streamed download + integrity verification,
filename safety, the once-a-day / skip-version throttle, the release-time
manifest maker + version guard, and the GUI wiring (checked structurally via
AST so the file never imports tkinter).
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.core import updates
from src.core.updates import (
    STATUS_DISABLED,
    STATUS_ERROR,
    STATUS_UP_TO_DATE,
    STATUS_UPDATE_AVAILABLE,
    UpdateError,
    UpdateInfo,
)

_GOOD_SHA = "a" * 64


def _manifest(**overrides) -> dict:
    payload = {
        "version": "99.0.0",
        "url": "https://github.com/Abe-Borg/Claude-Spec-Critic/releases/download/v99.0.0/SpecCriticSetup.exe",
        "sha256": _GOOD_SHA,
        "notes": "Shiny new release.",
        "published_at": "2026-07-17",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# Version comparison
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate, current, expected",
    [
        ("1.0.0", "1.0.0rc1", True),      # final beats its own rc
        ("1.0.0rc2", "1.0.0rc1", True),   # later rc beats earlier rc
        ("1.0.0rc1", "1.0.0rc2", False),
        ("1.0.0rc1", "1.0.0", False),     # rc never beats the final
        ("1.0.1", "1.0.0", True),
        ("1.1.0", "1.0.9", True),
        ("2.0.0", "1.9.9", True),
        ("1.0.0", "1.0.0", False),        # equal is not newer
        ("0.9.9", "1.0.0rc1", False),
        ("10.0.0", "9.0.0", True),        # numeric, not lexicographic
    ],
)
def test_is_newer(candidate: str, current: str, expected: bool) -> None:
    assert updates.is_newer(candidate, current) is expected


def test_parse_version_orders_rc_below_final() -> None:
    assert updates.parse_version("1.0.0rc1") < updates.parse_version("1.0.0rc2")
    assert updates.parse_version("1.0.0rc9") < updates.parse_version("1.0.0")


@pytest.mark.parametrize("bad", ["", "1.0", "1.0.0.0", "v1.0.0", "1.0.0beta1", "1.0.0-rc1", "abc"])
def test_parse_version_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        updates.parse_version(bad)


# --------------------------------------------------------------------------
# Manifest validation
# --------------------------------------------------------------------------


def test_parse_manifest_happy_path() -> None:
    info = updates.parse_manifest(_manifest())
    assert info.version == "99.0.0"
    assert info.sha256 == _GOOD_SHA
    assert info.notes == "Shiny new release."
    assert info.published_at == "2026-07-17"


def test_parse_manifest_lowercases_sha() -> None:
    info = updates.parse_manifest(_manifest(sha256="A" * 64))
    assert info.sha256 == "a" * 64


def test_parse_manifest_rejects_http_url() -> None:
    with pytest.raises(UpdateError):
        updates.parse_manifest(_manifest(url="http://example.com/x.exe"))


def test_parse_manifest_rejects_missing_url() -> None:
    payload = _manifest()
    del payload["url"]
    with pytest.raises(UpdateError):
        updates.parse_manifest(payload)


def test_parse_manifest_rejects_bad_sha() -> None:
    for bad in ["", "xyz", "a" * 63, "g" * 64]:
        with pytest.raises(UpdateError):
            updates.parse_manifest(_manifest(sha256=bad))


def test_parse_manifest_rejects_malformed_version() -> None:
    with pytest.raises(UpdateError):
        updates.parse_manifest(_manifest(version="not-a-version"))


def test_parse_manifest_rejects_non_object() -> None:
    with pytest.raises(UpdateError):
        updates.parse_manifest(["not", "a", "dict"])  # type: ignore[arg-type]


def test_parse_manifest_defaults_optional_fields() -> None:
    payload = _manifest()
    del payload["notes"]
    del payload["published_at"]
    info = updates.parse_manifest(payload)
    assert info.notes == ""
    assert info.published_at == ""


# --------------------------------------------------------------------------
# check_for_update — never raises
# --------------------------------------------------------------------------


def test_check_reports_update_available() -> None:
    result = updates.check_for_update(
        "1.0.0rc1", fetcher=lambda url, **kw: _manifest(version="1.0.0")
    )
    assert result.status == STATUS_UPDATE_AVAILABLE
    assert result.update_available
    assert result.info is not None and result.info.version == "1.0.0"


def test_check_reports_up_to_date() -> None:
    result = updates.check_for_update(
        "99.0.0", fetcher=lambda url, **kw: _manifest(version="99.0.0")
    )
    assert result.status == STATUS_UP_TO_DATE
    assert not result.update_available


def test_check_swallows_fetch_error() -> None:
    def boom(url, **kw):
        raise OSError("network down")

    result = updates.check_for_update("1.0.0", fetcher=boom)
    assert result.status == STATUS_ERROR
    assert result.error and "network down" in result.error
    assert result.info is None


def test_check_swallows_bad_manifest() -> None:
    result = updates.check_for_update(
        "1.0.0", fetcher=lambda url, **kw: _manifest(url="http://insecure/x.exe")
    )
    assert result.status == STATUS_ERROR


def test_check_disabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("SPEC_CRITIC_DISABLE_UPDATE_CHECK", "1")

    def should_not_run(url, **kw):  # pragma: no cover - must never be called
        raise AssertionError("fetcher ran while disabled")

    result = updates.check_for_update("1.0.0", fetcher=should_not_run)
    assert result.status == STATUS_DISABLED


@pytest.mark.parametrize("token", ["0", "false", "no", "off", "", "  "])
def test_disable_off_tokens_keep_checking(monkeypatch, token: str) -> None:
    monkeypatch.setenv("SPEC_CRITIC_DISABLE_UPDATE_CHECK", token)
    assert updates.update_check_disabled() is False


def test_check_passes_manifest_url(monkeypatch) -> None:
    seen = {}

    def spy(url, **kw):
        seen["url"] = url
        return _manifest(version="0.0.1")

    monkeypatch.setenv("SPEC_CRITIC_UPDATE_URL", "https://example.test/latest.json")
    updates.check_for_update("1.0.0", fetcher=spy)
    assert seen["url"] == "https://example.test/latest.json"


def test_manifest_url_default_points_at_repo() -> None:
    url = updates.manifest_url()
    assert url.startswith("https://github.com/")
    assert updates.GITHUB_OWNER in url and updates.GITHUB_REPO in url
    assert url.endswith("latest.json")


# --------------------------------------------------------------------------
# Throttle-state / download paths
# --------------------------------------------------------------------------


def test_default_state_path_env_override(monkeypatch, tmp_path: Path) -> None:
    override = tmp_path / "custom" / "state.json"
    monkeypatch.setenv("SPEC_CRITIC_UPDATE_STATE_PATH", str(override))
    assert updates.default_state_path() == override


def test_default_state_path_in_dot_dir(monkeypatch) -> None:
    monkeypatch.delenv("SPEC_CRITIC_UPDATE_STATE_PATH", raising=False)
    path = updates.default_state_path()
    assert path.parent.name == ".spec_critic"
    assert path.name == "update_check.json"


def test_default_download_dir_in_dot_dir() -> None:
    path = updates.default_download_dir()
    assert path.parent.name == ".spec_critic"
    assert path.name == "updates"


# --------------------------------------------------------------------------
# Download + integrity
# --------------------------------------------------------------------------


class _FakeResponse:
    """A minimal stand-in for a urllib response usable as a context manager."""

    def __init__(self, data: bytes, *, content_length: bool = True):
        self._buf = io.BytesIO(data)
        self._headers = {"Content-Length": str(len(data))} if content_length else {}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener_for(data: bytes, *, content_length: bool = True):
    def _open(url, *, timeout=None):
        return _FakeResponse(data, content_length=content_length)

    return _open


def test_download_installer_verifies_and_writes(tmp_path: Path) -> None:
    payload = b"pretend installer bytes" * 100
    sha = hashlib.sha256(payload).hexdigest()
    info = UpdateInfo(version="99.0.0", url="https://host/SpecCriticSetup.exe", sha256=sha)

    seen: list[tuple[int, int]] = []
    dest = updates.download_installer(
        info, tmp_path, opener=_opener_for(payload), progress=lambda d, t: seen.append((d, t))
    )
    assert dest.exists()
    assert dest.read_bytes() == payload
    assert dest.name == "SpecCriticSetup.exe"
    # progress was reported and the final tally equals the payload size
    assert seen and seen[-1][0] == len(payload)
    assert seen[-1][1] == len(payload)


def test_download_installer_checksum_mismatch_deletes_file(tmp_path: Path) -> None:
    payload = b"tampered bytes"
    info = UpdateInfo(version="99.0.0", url="https://host/SpecCriticSetup.exe", sha256=_GOOD_SHA)
    with pytest.raises(UpdateError):
        updates.download_installer(info, tmp_path, opener=_opener_for(payload))
    # the failed download must not be left behind to be run by mistake
    assert not (tmp_path / "SpecCriticSetup.exe").exists()


def test_download_installer_rejects_http(tmp_path: Path) -> None:
    info = UpdateInfo(version="99.0.0", url="http://host/x.exe", sha256=_GOOD_SHA)
    with pytest.raises(UpdateError):
        updates.download_installer(info, tmp_path, opener=_opener_for(b"x"))


def test_download_handles_missing_content_length(tmp_path: Path) -> None:
    payload = b"no length header here"
    sha = hashlib.sha256(payload).hexdigest()
    info = UpdateInfo(version="99.0.0", url="https://host/SpecCriticSetup.exe", sha256=sha)
    seen: list[tuple[int, int]] = []
    updates.download_installer(
        info, tmp_path,
        opener=_opener_for(payload, content_length=False),
        progress=lambda d, t: seen.append((d, t)),
    )
    assert seen[-1] == (len(payload), 0)  # unknown total reported as 0


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://host/SpecCriticSetup.exe", "SpecCriticSetup.exe"),
        ("https://host/path/to/Custom.exe", "Custom.exe"),
        ("https://host/SpecCriticSetup.exe?token=abc", "SpecCriticSetup.exe"),
        ("https://host/no-extension", "SpecCriticSetup.exe"),      # fallback
        ("https://host/../../evil.exe", "evil.exe"),               # traversal stripped
        ("https://host/", "SpecCriticSetup.exe"),
    ],
)
def test_installer_filename_is_safe(url: str, expected: str) -> None:
    assert updates._installer_filename(url) == expected


def test_verify_sha256_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello world")
    sha = hashlib.sha256(b"hello world").hexdigest()
    assert updates.verify_sha256(p, sha) == sha
    with pytest.raises(UpdateError):
        updates.verify_sha256(p, _GOOD_SHA)


# --------------------------------------------------------------------------
# Throttle state (once-a-day + skip-version)
# --------------------------------------------------------------------------


def test_should_auto_check_when_never_checked() -> None:
    assert updates.should_auto_check({}, now=datetime(2026, 7, 17, 9, 0)) is True


def test_should_auto_check_respects_interval() -> None:
    now = datetime(2026, 7, 17, 12, 0)
    fresh = updates.record_check({}, now=now - timedelta(hours=2))
    assert updates.should_auto_check(fresh, now=now) is False
    stale = updates.record_check({}, now=now - timedelta(days=2))
    assert updates.should_auto_check(stale, now=now) is True


def test_should_auto_check_tolerates_corrupt_timestamp() -> None:
    assert updates.should_auto_check({"last_check": "not-a-date"}, now=datetime(2026, 7, 17)) is True


def test_state_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "update_check.json"
    state = updates.record_check({}, now=datetime(2026, 7, 17, 9, 0))
    updates.mark_skipped(state, "3.5.0")
    updates.save_state(path, state)

    loaded = updates.load_state(path)
    assert loaded["skipped_version"] == "3.5.0"
    assert updates.version_is_skipped(loaded, "3.5.0")
    assert not updates.version_is_skipped(loaded, "3.4.0")


def test_load_state_tolerates_missing_and_corrupt(tmp_path: Path) -> None:
    assert updates.load_state(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert updates.load_state(bad) == {}


def test_save_state_is_nonfatal_on_bad_path(tmp_path: Path) -> None:
    # A path whose parent is a file (not a dir) can't be written; must not raise.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    updates.save_state(blocker / "child.json", {"a": 1})  # no exception


# --------------------------------------------------------------------------
# Release-time manifest maker round-trips through the runtime parser
# --------------------------------------------------------------------------


def _load_packaging_module(name: str):
    import importlib.util

    script = (
        Path(__file__).resolve().parent.parent
        / "packaging" / "windows" / f"{name}.py"
    )
    spec = importlib.util.spec_from_file_location(name, script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_make_manifest_script_roundtrips(tmp_path: Path) -> None:
    """The CI manifest generator emits exactly what parse_manifest accepts."""
    module = _load_packaging_module("make_manifest")

    installer = tmp_path / "SpecCriticSetup.exe"
    installer.write_bytes(b"installer payload")
    out = tmp_path / "latest.json"
    module.write_manifest(
        version="3.1.0",
        installer=installer,
        url="https://github.com/Abe-Borg/Claude-Spec-Critic/releases/download/v3.1.0/SpecCriticSetup.exe",
        out_path=out,
        notes="Release 3.1.0",
        published_at="2026-07-17",
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    info = updates.parse_manifest(payload)  # must not raise
    assert info.version == "3.1.0"
    assert info.sha256 == hashlib.sha256(b"installer payload").hexdigest()


# --------------------------------------------------------------------------
# Manifest transport must be https too (the sha256 root of trust)
# --------------------------------------------------------------------------


def test_fetch_manifest_rejects_non_https() -> None:
    # Raises before any network call — the manifest is the root of trust, so a
    # SPEC_CRITIC_UPDATE_URL override cannot downgrade it to http.
    for bad in ["http://updates.lan/latest.json", "ftp://x/latest.json", "file:///etc/x"]:
        with pytest.raises(UpdateError):
            updates.fetch_manifest(bad)


# --------------------------------------------------------------------------
# An interrupted download leaves nothing behind (atomic .part -> final)
# --------------------------------------------------------------------------


class _BrokenResponse:
    """A response whose read() raises partway through (dropped connection)."""

    def __init__(self, data: bytes, *, fail_after: int):
        self._buf = io.BytesIO(data)
        self._fail_after = fail_after
        self._reads = 0

    def read(self, n: int = -1) -> bytes:
        if self._reads >= self._fail_after:
            raise OSError("connection reset by peer")
        self._reads += 1
        return self._buf.read(n)

    def getheader(self, name, default=None):
        return {"Content-Length": "999999"}.get(name, default)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_interrupted_leaves_no_file(tmp_path: Path) -> None:
    info = UpdateInfo(version="99.0.0", url="https://host/SpecCriticSetup.exe", sha256=_GOOD_SHA)

    def opener(url, *, timeout=None):
        return _BrokenResponse(b"x" * 10000, fail_after=2)

    with pytest.raises(OSError):
        updates.download_installer(info, tmp_path, opener=opener, chunk=1024)
    # Neither the final installer nor the .part temp survives the interruption.
    assert not (tmp_path / "SpecCriticSetup.exe").exists()
    assert not (tmp_path / "SpecCriticSetup.exe.part").exists()


def test_download_success_leaves_no_part_file(tmp_path: Path) -> None:
    payload = b"complete installer" * 50
    sha = hashlib.sha256(payload).hexdigest()
    info = UpdateInfo(version="99.0.0", url="https://host/SpecCriticSetup.exe", sha256=sha)
    dest = updates.download_installer(info, tmp_path, opener=_opener_for(payload))
    assert dest.read_bytes() == payload
    assert not (tmp_path / "SpecCriticSetup.exe.part").exists()


# --------------------------------------------------------------------------
# Release version guard (packaging/windows/check_release_version.py)
# --------------------------------------------------------------------------


def test_release_guard_accepts_matching_tag() -> None:
    from src import __version__

    guard = _load_packaging_module("check_release_version")
    # Both the current pyproject and __init__ literals must equal the tag; they
    # are kept in lockstep, so the real repo version passes with and without "v".
    assert guard.check(f"v{__version__}") == []
    assert guard.check(__version__) == []


def test_release_guard_rejects_mismatched_tag() -> None:
    guard = _load_packaging_module("check_release_version")
    problems = guard.check("v99.99.99")
    # Both literals disagree with a bogus tag, so both are reported.
    assert len(problems) == 2
    assert any("pyproject.toml" in p for p in problems)
    assert any("__init__.py" in p for p in problems)


# --------------------------------------------------------------------------
# GUI wiring — checked structurally (gui.py / update_controller.py can't
# import without customtkinter, so this file parses their source via AST and
# never needs the tkinter-availability skip list in conftest.py).
# --------------------------------------------------------------------------


_GUI_DIR = Path(__file__).resolve().parent.parent / "src" / "gui"


def _gui_source() -> str:
    return (_GUI_DIR / "gui.py").read_text(encoding="utf-8")


def _controller_source() -> str:
    return (_GUI_DIR / "update_controller.py").read_text(encoding="utf-8")


def _app_methods() -> set[str]:
    tree = ast.parse(_gui_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "SpecReviewApp":
            return {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
    raise AssertionError("SpecReviewApp not found")


def test_gui_imports_update_controller() -> None:
    assert "update_controller" in _gui_source()


def test_gui_defines_update_methods() -> None:
    methods = _app_methods()
    assert {
        "_build_footer",
        "_maybe_auto_check_for_updates",
        "_on_check_for_updates_clicked",
        "_start_update_check",
        "_on_update_check_done",
        "_show_update_dialog",
        "_close_update_dialog",
    } <= methods


def test_gui_schedules_auto_check() -> None:
    src = _gui_source()
    assert "_maybe_auto_check_for_updates" in src
    assert "_build_footer" in src
    assert "init_update_state" in src


def test_controller_calls_updater_module() -> None:
    src = _controller_source()
    assert "updates.check_for_update" in src
    assert "updates.download_installer" in src
    assert "updates.spawn_installer" in src
    # The download destination comes from the updater module, not ad-hoc paths.
    assert "updates.default_download_dir()" in src


def test_controller_guards_download_lifecycle() -> None:
    src = _controller_source()
    # Concurrent-download guard + dismissed-dialog cancellation flags.
    assert "_update_downloading" in src
    assert "_update_download_cancelled" in src
    # Install is refused while a review run is in flight.
    assert "is_processing" in src
