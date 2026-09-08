"""Windows packaging entry point + bundle contracts (hermetic).

Covers the ``packaging/windows/`` pieces behind two fixes:

* **No first-use tokenizer download.** ``bundle_assets.tiktoken_cache_datas``
  turns a pre-warmed tiktoken cache directory into PyInstaller ``datas``
  entries under the fixed bundle folder and fails loudly (naming the env var)
  for a missing, empty, rank-file-less, or corrupt directory;
  ``app_entry.configure_tiktoken_cache`` points ``TIKTOKEN_CACHE_DIR`` at
  that folder before any ``src`` import when frozen (unfrozen runs untouched,
  an operator's value respected); ``--selfcheck`` reports a positive token
  count in the line the release workflow's smoke step parses.
* **Long paths + icon.** ``spec-critic.manifest`` carries
  ``longPathAware=true`` alongside PyInstaller's default entries, the spec
  embeds it, and the icon is conditional on a not-yet-supplied ``.ico``.

The packaging modules are scripts, not part of ``src``; they are loaded from
their file paths (the pattern ``tests/test_updates.py`` uses for
``make_manifest`` / ``check_release_version``).
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import re
import subprocess
import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PACKAGING = _REPO_ROOT / "packaging" / "windows"
_SPEC = _PACKAGING / "spec-critic.spec"
_MANIFEST = _PACKAGING / "spec-critic.manifest"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release.yml"

_WS2 = "http://schemas.microsoft.com/SMI/2016/WindowsSettings"
_ASM_V3 = "urn:schemas-microsoft-com:asm.v3"
_COMPAT_V1 = "urn:schemas-microsoft-com:compatibility.v1"
_ASM_V1 = "urn:schemas-microsoft-com:asm.v1"
# The supportedOS list in PyInstaller 6.11.1's built-in manifest template
# (Vista, 7, 8, 8.1, 10/11). A supplied manifest REPLACES that template, so
# these must be carried explicitly.
_PYINSTALLER_DEFAULT_SUPPORTED_OS = {
    "{e2011457-1546-43c5-a5fe-008deee3d3f0}",
    "{35138b9a-5d96-4fbd-8e2d-a2440225f93a}",
    "{4a2f28e3-53b9-4441-ba9c-d69d4a4a6e38}",
    "{1f676c76-80e1-4239-95bb-83d0f6d0da78}",
    "{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}",
}


def _load_packaging_module(name: str):
    script = _PACKAGING / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"packaging_windows_{name}", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def bundle_assets():
    return _load_packaging_module("bundle_assets")


@pytest.fixture
def app_entry():
    return _load_packaging_module("app_entry")


def _fake_warm_cache(tmp_path: Path, bundle_assets, content: bytes = b"fake rank bytes"):
    """A cache dir holding a stand-in rank file under tiktoken's sha1(url) name."""
    cache = tmp_path / "tiktoken_cache"
    cache.mkdir()
    rank = cache / bundle_assets.cl100k_cache_filename()
    rank.write_bytes(content)
    return cache, rank, hashlib.sha256(content).hexdigest()


# --------------------------------------------------------------------------
# bundle_assets.tiktoken_cache_datas
# --------------------------------------------------------------------------


class TestTiktokenCacheDatas:
    def test_populated_dir_yields_datas_under_bundle_folder(self, tmp_path, bundle_assets):
        cache, rank, digest = _fake_warm_cache(tmp_path, bundle_assets)
        other = cache / "0123456789abcdef0123456789abcdef01234567"
        other.write_bytes(b"another encoding")
        # tiktoken's write-in-progress leftover must never ship.
        (cache / f"{rank.name}.deadbeef.tmp").write_bytes(b"partial")

        entries = bundle_assets.tiktoken_cache_datas(cache, expected_sha256=digest)

        assert entries == sorted(entries), "entries must be sorted for a reproducible spec"
        assert [dest for _, dest in entries] == [bundle_assets.TIKTOKEN_CACHE_BUNDLE_DIR] * 2
        assert {Path(src) for src, _ in entries} == {rank.resolve(), other.resolve()}
        assert all(Path(src).is_absolute() for src, _ in entries)

    def test_missing_dir_raises_naming_env_var(self, tmp_path, bundle_assets):
        missing = tmp_path / "nope"
        with pytest.raises(bundle_assets.BundleAssetError) as excinfo:
            bundle_assets.tiktoken_cache_datas(missing)
        message = str(excinfo.value)
        assert bundle_assets.TIKTOKEN_CACHE_SRC_ENV in message
        assert str(missing) in message
        assert "does not exist" in message
        assert "TIKTOKEN_CACHE_DIR" in message  # the warm hint

    def test_empty_dir_raises_naming_env_var(self, tmp_path, bundle_assets):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(bundle_assets.BundleAssetError) as excinfo:
            bundle_assets.tiktoken_cache_datas(empty)
        message = str(excinfo.value)
        assert bundle_assets.TIKTOKEN_CACHE_SRC_ENV in message
        assert "empty" in message
        assert "first use" in message

    def test_only_tmp_leftovers_counts_as_empty(self, tmp_path, bundle_assets):
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "abc.tmp").write_bytes(b"partial")
        with pytest.raises(bundle_assets.BundleAssetError, match="empty"):
            bundle_assets.tiktoken_cache_datas(cache)

    def test_file_instead_of_dir_raises(self, tmp_path, bundle_assets):
        not_a_dir = tmp_path / "file"
        not_a_dir.write_bytes(b"x")
        with pytest.raises(bundle_assets.BundleAssetError, match="not a directory"):
            bundle_assets.tiktoken_cache_datas(not_a_dir)

    def test_dir_without_rank_file_raises(self, tmp_path, bundle_assets):
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "some-other-file").write_bytes(b"x")
        with pytest.raises(bundle_assets.BundleAssetError) as excinfo:
            bundle_assets.tiktoken_cache_datas(cache)
        message = str(excinfo.value)
        assert bundle_assets.cl100k_cache_filename() in message
        assert bundle_assets.TIKTOKEN_CACHE_SRC_ENV in message

    def test_corrupt_rank_file_raises_on_hash_mismatch(self, tmp_path, bundle_assets):
        cache, rank, _ = _fake_warm_cache(tmp_path, bundle_assets, content=b"not the real ranks")
        # Default expected hash is the one tiktoken verifies at runtime.
        with pytest.raises(bundle_assets.BundleAssetError) as excinfo:
            bundle_assets.tiktoken_cache_datas(cache)
        message = str(excinfo.value)
        assert "sha256" in message
        assert str(rank) in message
        assert "re-download" in message

    def test_hash_check_can_be_skipped(self, tmp_path, bundle_assets):
        cache, rank, _ = _fake_warm_cache(tmp_path, bundle_assets)
        entries = bundle_assets.tiktoken_cache_datas(cache, expected_sha256=None)
        assert entries == [(str(rank.resolve()), bundle_assets.TIKTOKEN_CACHE_BUNDLE_DIR)]

    def test_env_var_selects_source_dir(self, tmp_path, bundle_assets):
        cache, rank, digest = _fake_warm_cache(tmp_path, bundle_assets)
        environ = {bundle_assets.TIKTOKEN_CACHE_SRC_ENV: str(cache)}
        entries = bundle_assets.tiktoken_cache_datas(environ=environ, expected_sha256=digest)
        assert entries == [(str(rank.resolve()), bundle_assets.TIKTOKEN_CACHE_BUNDLE_DIR)]

    def test_explicit_argument_beats_env_var(self, tmp_path, bundle_assets):
        cache, _, _ = _fake_warm_cache(tmp_path, bundle_assets)
        environ = {bundle_assets.TIKTOKEN_CACHE_SRC_ENV: str(tmp_path / "elsewhere")}
        assert bundle_assets.resolve_tiktoken_cache_src(cache, environ=environ) == cache

    def test_default_source_is_build_tiktoken_cache(self, bundle_assets):
        resolved = bundle_assets.resolve_tiktoken_cache_src(environ={})
        assert resolved == _REPO_ROOT / "build" / "tiktoken_cache"
        # Blank env var falls through to the default too.
        blank = {bundle_assets.TIKTOKEN_CACHE_SRC_ENV: "   "}
        assert bundle_assets.resolve_tiktoken_cache_src(environ=blank) == resolved

    def test_cli_main_fails_loudly_on_a_missing_cache(self, tmp_path, bundle_assets, capsys):
        # release.yml runs this right after the warm step so a bad cache fails
        # there, with the env var named, rather than inside PyInstaller.
        assert bundle_assets.main([str(tmp_path / "missing")]) == 1
        captured = capsys.readouterr()
        assert captured.err.startswith("ERROR:")
        assert bundle_assets.TIKTOKEN_CACHE_SRC_ENV in captured.err
        assert captured.out == ""

    def test_cli_main_lists_entries_on_success(self, monkeypatch, bundle_assets, capsys):
        entries = [("/warm/9b5ad71b", "tiktoken_cache"), ("/warm/other", "tiktoken_cache")]
        monkeypatch.setattr(bundle_assets, "tiktoken_cache_datas", lambda src_dir: entries)
        assert bundle_assets.main([]) == 0
        captured = capsys.readouterr()
        assert "/warm/9b5ad71b -> tiktoken_cache/" in captured.out
        assert "tiktoken cache OK: 2 file(s)" in captured.out
        assert captured.err == ""

    def test_constants_come_from_the_app_tokenizer(self, bundle_assets):
        from src.core import tokenizer

        assert bundle_assets.CL100K_BASE_SHA256 == tokenizer.CL100K_BASE_SHA256
        assert bundle_assets.cl100k_cache_filename() == tokenizer.cl100k_cache_filename()
        assert bundle_assets.TIKTOKEN_CACHE_DIR_ENV == tokenizer.TIKTOKEN_CACHE_DIR_ENV


# --------------------------------------------------------------------------
# app_entry.configure_tiktoken_cache — frozen vs. unfrozen
# --------------------------------------------------------------------------


class TestConfigureTiktokenCache:
    def test_frozen_sets_bundled_dir(self, monkeypatch, tmp_path, app_entry):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        env: dict[str, str] = {}
        expected = os.path.join(str(tmp_path), app_entry.TIKTOKEN_CACHE_BUNDLE_DIR)
        assert app_entry.configure_tiktoken_cache(env) == expected
        assert env == {"TIKTOKEN_CACHE_DIR": expected}

    def test_frozen_defaults_to_os_environ(self, monkeypatch, tmp_path, app_entry):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
        app_entry.configure_tiktoken_cache()
        assert os.environ["TIKTOKEN_CACHE_DIR"] == os.path.join(
            str(tmp_path), app_entry.TIKTOKEN_CACHE_BUNDLE_DIR
        )

    def test_unfrozen_leaves_env_untouched(self, monkeypatch, app_entry):
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        env: dict[str, str] = {}
        assert app_entry.configure_tiktoken_cache(env) is None
        assert env == {}
        assert app_entry.frozen_bundle_dir() is None

    def test_existing_value_is_respected(self, monkeypatch, tmp_path, app_entry):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        env = {"TIKTOKEN_CACHE_DIR": "C:/operator/cache"}
        assert app_entry.configure_tiktoken_cache(env) == "C:/operator/cache"
        assert env == {"TIKTOKEN_CACHE_DIR": "C:/operator/cache"}

    def test_frozen_without_meipass_falls_back_to_exe_dir(self, monkeypatch, app_entry):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        assert app_entry.frozen_bundle_dir() == os.path.dirname(os.path.abspath(sys.executable))

    def test_bundle_folder_pinned_in_lockstep_with_bundle_assets(self, app_entry, bundle_assets):
        # app_entry duplicates the folder name (it must import nothing at
        # module load); this is the lockstep guard.
        assert app_entry.TIKTOKEN_CACHE_BUNDLE_DIR == bundle_assets.TIKTOKEN_CACHE_BUNDLE_DIR
        assert app_entry.TIKTOKEN_CACHE_BUNDLE_DIR == "tiktoken_cache"

    def test_env_var_name_pinned_to_tokenizer(self, app_entry):
        from src.core import tokenizer

        assert app_entry.TIKTOKEN_CACHE_DIR_ENV == tokenizer.TIKTOKEN_CACHE_DIR_ENV == "TIKTOKEN_CACHE_DIR"


class TestCacheConfiguredBeforeSrcImport:
    """The env var must be set before ``src`` is imported — three angles."""

    def test_no_module_level_src_import(self):
        tree = ast.parse((_PACKAGING / "app_entry.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                assert not any(a.name.split(".")[0] == "src" for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "src"

    def test_main_configures_cache_first(self):
        tree = ast.parse((_PACKAGING / "app_entry.py").read_text(encoding="utf-8"))
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        first = main.body[0]
        assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
        assert isinstance(first.value.func, ast.Name)
        assert first.value.func.id == "configure_tiktoken_cache"

    def test_env_is_set_when_src_is_first_imported(self, tmp_path):
        """Functional: a meta-path spy records TIKTOKEN_CACHE_DIR at the moment
        ``src`` is first imported by ``main(["--version"])`` in a frozen-shaped
        subprocess (a fresh interpreter, so ``src`` is not yet in sys.modules)."""
        script = f"""
import importlib.util, os, sys
sys.frozen = True
sys._MEIPASS = {str(tmp_path)!r}
seen = {{}}
class Spy:
    def find_spec(self, name, path=None, target=None):
        if name == "src" or name.startswith("src."):
            seen.setdefault("env", os.environ.get("TIKTOKEN_CACHE_DIR", "<unset>"))
        return None
sys.meta_path.insert(0, Spy())
assert "src" not in sys.modules
sys.path.insert(0, {str(_REPO_ROOT)!r})
spec = importlib.util.spec_from_file_location("app_entry", {str(_PACKAGING / "app_entry.py")!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert "src" not in sys.modules, "app_entry imported src at module load"
rc = mod.main(["--version"])
print("ENV_AT_FIRST_SRC_IMPORT=" + seen.get("env", "<never imported src>"))
print("RC=" + str(rc))
"""
        env = {k: v for k, v in os.environ.items() if k not in ("TIKTOKEN_CACHE_DIR", "SPEC_CRITIC_SELFCHECK_OUT")}
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=str(_REPO_ROOT),
        )
        assert proc.returncode == 0, proc.stderr
        recorded = re.search(r"^ENV_AT_FIRST_SRC_IMPORT=(.*)$", proc.stdout, re.M)
        assert recorded is not None, proc.stdout
        assert recorded.group(1) == os.path.join(str(tmp_path), "tiktoken_cache")
        assert "RC=0" in proc.stdout


# --------------------------------------------------------------------------
# --selfcheck tokenizer probe
# --------------------------------------------------------------------------


def _fake_probe(tokens: int = 7) -> dict[str, object]:
    return {
        "encoding": "cl100k_base",
        "tokens": tokens,
        "rank_file_present": True,
        "cache_dir": r"C:\Program Files\Spec Critic\_internal\tiktoken_cache",
    }


class TestSelfcheckProbe:
    def test_probe_line_matches_the_workflow_regexes(self, app_entry):
        line = app_entry.format_tokenizer_probe(_fake_probe(11))
        # The exact patterns release.yml's smoke step applies.
        assert re.search(r"tokens=(\d+)", line).group(1) == "11"
        assert re.search(r"rank_file_present=True", line)
        assert re.search(r"cache_dir=.*tiktoken_cache", line)
        assert line.startswith("tokenizer: cl100k_base ")
        # cache_dir (may contain spaces) is last so the other fields parse cleanly.
        assert line.index("cache_dir=") > line.index("rank_file_present=")

    def test_probe_counts_positive_tokens(self, app_entry):
        from src.core.tokenizer import EncoderLoadError

        try:
            probe = app_entry.tokenizer_probe()
        except EncoderLoadError as exc:
            pytest.skip(
                "cl100k_base rank file is not available offline in this environment "
                f"(tiktoken fetches it from the network on first use): {str(exc)[:200]}"
            )
        assert probe["encoding"] == "cl100k_base"
        assert isinstance(probe["tokens"], int) and probe["tokens"] > 0
        assert isinstance(probe["rank_file_present"], bool)
        assert probe["cache_dir"]

    @pytest.fixture
    def stub_gui_import(self, monkeypatch):
        # _selfcheck imports src.gui.gui (customtkinter); stub it so the
        # composition test is hermetic on hosts without Tk.
        monkeypatch.setitem(sys.modules, "src.gui.gui", types.ModuleType("src.gui.gui"))

    def test_selfcheck_emits_probe_line_and_exits_zero(self, monkeypatch, tmp_path, app_entry, stub_gui_import):
        monkeypatch.setattr(app_entry, "tokenizer_probe", lambda: _fake_probe(7))
        out = tmp_path / "selfcheck.txt"
        monkeypatch.setenv("SPEC_CRITIC_SELFCHECK_OUT", str(out))

        assert app_entry._selfcheck() == 0

        text = out.read_text(encoding="utf-8")
        assert "selfcheck ok" in text
        assert "tokens=7" in text
        assert "rank_file_present=True" in text
        assert "cache_dir=" in text

    def test_selfcheck_fails_when_probe_raises(self, monkeypatch, tmp_path, app_entry, stub_gui_import):
        def boom():
            raise RuntimeError("rank file unreachable")

        monkeypatch.setattr(app_entry, "tokenizer_probe", boom)
        out = tmp_path / "selfcheck.txt"
        monkeypatch.setenv("SPEC_CRITIC_SELFCHECK_OUT", str(out))

        assert app_entry._selfcheck() == 1
        text = out.read_text(encoding="utf-8")
        assert text.startswith("SELFCHECK FAILED: tokenizer probe")
        assert "rank file unreachable" in text

    def test_selfcheck_fails_on_zero_count(self, monkeypatch, tmp_path, app_entry, stub_gui_import):
        monkeypatch.setattr(app_entry, "tokenizer_probe", lambda: _fake_probe(0))
        out = tmp_path / "selfcheck.txt"
        monkeypatch.setenv("SPEC_CRITIC_SELFCHECK_OUT", str(out))

        assert app_entry._selfcheck() == 1
        assert "counted 0 tokens" in out.read_text(encoding="utf-8")

    def test_main_accepts_explicit_argv(self, monkeypatch, tmp_path, app_entry):
        from src import __version__

        out = tmp_path / "version.txt"
        monkeypatch.setenv("SPEC_CRITIC_SELFCHECK_OUT", str(out))
        assert app_entry.main(["--version"]) == 0
        assert out.read_text(encoding="utf-8").strip() == __version__


# --------------------------------------------------------------------------
# spec-critic.manifest — longPathAware + PyInstaller default parity
# --------------------------------------------------------------------------


class TestManifest:
    def _root(self):
        return ET.parse(_MANIFEST).getroot()

    def test_long_path_aware_is_true(self):
        root = self._root()
        assert root.tag == f"{{{_ASM_V1}}}assembly"
        settings = root.find(f"{{{_ASM_V3}}}application/{{{_ASM_V3}}}windowsSettings")
        assert settings is not None, "windowsSettings block missing"
        el = settings.find(f"{{{_WS2}}}longPathAware")
        assert el is not None, "longPathAware missing from windowsSettings"
        assert (el.text or "").strip().lower() == "true"
        # The ws2: prefix form the Microsoft docs use.
        assert "<ws2:longPathAware>true</ws2:longPathAware>" in _MANIFEST.read_text(encoding="utf-8")

    def test_keeps_pyinstaller_default_entries(self):
        root = self._root()
        supported = {
            el.attrib["Id"]
            for el in root.iter(f"{{{_COMPAT_V1}}}supportedOS")
        }
        assert supported == _PYINSTALLER_DEFAULT_SUPPORTED_OS
        level = root.find(f".//{{{_ASM_V3}}}requestedExecutionLevel")
        assert level is not None
        assert level.attrib["level"] == "asInvoker"
        assert level.attrib["uiAccess"] == "false"
        identities = [el for el in root.iter(f"{{{_ASM_V1}}}assemblyIdentity")]
        common_controls = [
            el for el in identities if el.attrib.get("name") == "Microsoft.Windows.Common-Controls"
        ]
        assert len(common_controls) == 1
        assert common_controls[0].attrib["version"] == "6.0.0.0"
        assert common_controls[0].attrib["publicKeyToken"] == "6595b64144ccf1df"

    def test_no_dpi_awareness_entry(self):
        # customtkinter sets process DPI awareness at runtime; a manifest entry
        # would take precedence over that call.
        for el in self._root().iter():
            local = el.tag.rsplit("}", 1)[-1].lower()
            assert local not in {"dpiaware", "dpiawareness"}, el.tag

    def test_survives_pyinstaller_rewrite(self):
        """PyInstaller re-serializes a supplied manifest through minidom and
        re-injects the execution level + Common-Controls dependency; the
        long-path setting must survive and the dependency must not duplicate."""
        try:
            from PyInstaller.utils.win32 import winmanifest
        except Exception:  # pragma: no cover - PyInstaller is not a test dependency
            pytest.skip("PyInstaller not installed in this environment")
        out = winmanifest.create_application_manifest(_MANIFEST.read_bytes(), False, False)
        root = ET.fromstring(out)
        el = root.find(f".//{{{_WS2}}}longPathAware")
        assert el is not None and (el.text or "").strip() == "true"
        assert {e.attrib["Id"] for e in root.iter(f"{{{_COMPAT_V1}}}supportedOS")} == _PYINSTALLER_DEFAULT_SUPPORTED_OS
        assert len(list(root.iter(f"{{{_ASM_V1}}}dependency"))) == 1
        assert root.find(f".//{{{_ASM_V3}}}requestedExecutionLevel").attrib["level"] == "asInvoker"


# --------------------------------------------------------------------------
# spec-critic.spec wiring (parsed as Python; PyInstaller injects the globals)
# --------------------------------------------------------------------------


class TestSpecWiring:
    def _spec_text(self) -> str:
        return _SPEC.read_text(encoding="utf-8")

    def _exe_call(self) -> ast.Call:
        tree = ast.parse(self._spec_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "exe" for t in node.targets
            ):
                assert isinstance(node.value, ast.Call)
                assert isinstance(node.value.func, ast.Name) and node.value.func.id == "EXE"
                return node.value
        raise AssertionError("no `exe = EXE(...)` assignment in the spec")

    def test_exe_embeds_the_manifest(self):
        kwargs = {kw.arg: kw.value for kw in self._exe_call().keywords}
        assert "manifest" in kwargs
        assert isinstance(kwargs["manifest"], ast.Name) and kwargs["manifest"].id == "_manifest"
        assert 'spec-critic.manifest' in self._spec_text()
        assert _MANIFEST.is_file()

    def test_icon_is_conditional_on_the_ico_file(self):
        kwargs = {kw.arg: kw.value for kw in self._exe_call().keywords}
        assert isinstance(kwargs["icon"], ast.Name) and kwargs["icon"].id == "_icon"
        text = self._spec_text()
        assert "spec-critic.ico" in text
        assert "os.path.isfile(_icon_path)" in text
        assert "icon=None" not in text

    def test_spec_bundles_the_warmed_tiktoken_cache(self):
        text = self._spec_text()
        assert "from bundle_assets import tiktoken_cache_datas" in text
        assert "datas += tiktoken_cache_datas()" in text
        # The datas list is assembled before Analysis consumes it.
        assert text.index("datas += tiktoken_cache_datas()") < text.index("a = Analysis(")
        assert text.index("sys.path.insert(0, SPECPATH)") < text.index("from bundle_assets import")


# --------------------------------------------------------------------------
# release.yml — warm before PyInstaller, assert the probe after
# --------------------------------------------------------------------------


class TestReleaseWorkflow:
    def _text(self) -> str:
        return _WORKFLOW.read_text(encoding="utf-8")

    def test_warm_step_runs_between_install_and_pyinstaller(self):
        text = self._text()
        i_install = text.index("name: Install app + PyInstaller")
        i_warm = text.index("name: Warm the tiktoken cache")
        i_build = text.index("name: Build one-folder app (PyInstaller)")
        assert i_install < i_warm < i_build
        warm = text[i_warm:i_build]
        assert "TIKTOKEN_CACHE_DIR:" in warm
        assert "get_encoding('cl100k_base')" in warm
        assert "python packaging/windows/bundle_assets.py" in warm
        assert "SPEC_CRITIC_TIKTOKEN_CACHE_SRC" in text[:i_warm]  # job-level env

    def test_smoke_step_asserts_the_tokenizer_probe(self):
        text = self._text()
        i_smoke = text.index("name: Smoke - frozen exe self-check")
        i_next = text.index("name: Install Inno Setup")
        smoke = text[i_smoke:i_next]
        assert "tokens=(\\d+)" in smoke
        assert "rank_file_present=True" in smoke
        assert "cache_dir=.*tiktoken_cache" in smoke
        assert "--selfcheck" in smoke
        # The frozen app must set TIKTOKEN_CACHE_DIR itself; the smoke step
        # must neither export it in pwsh nor declare it as a step env key
        # (comments may mention it).
        assert "$env:TIKTOKEN_CACHE_DIR" not in smoke
        assert "TIKTOKEN_CACHE_DIR:" not in smoke

    def test_tokenizer_changes_trigger_the_packaging_build(self):
        text = self._text()
        paths = text[text.index("paths:"):text.index("workflow_dispatch:")]
        assert '"src/core/tokenizer.py"' in paths
