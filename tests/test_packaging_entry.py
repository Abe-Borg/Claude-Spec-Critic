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
* **License acceptance.** ``installer.iss`` shows the repository ``LICENSE``
  on Inno Setup's License Agreement page (the user must select "I accept the
  agreement" to continue), nothing in the script skips that page or accepts
  for the user, the file is installed beside the app as ``LICENSE.txt``, its
  encoding is one Inno can display, and a change to it rebuilds the installer.
* **Third-party notices.** ``third_party_notices`` lists every distribution
  that owns a file the analysis collected (plus PyInstaller, whose bootloader
  is the exe), with each one's license texts, the interpreter's
  ``LICENSE.txt``, and the Tcl/Tk terms; a bundled package without a license
  text, a missing interpreter license, or missing Tcl/Tk terms fails the build
  and leaves no notices file. The spec writes ``dist/THIRD-PARTY-NOTICES.txt``
  after the analysis, the installer installs it beside the app, the workflow
  checks the interpreter and Tcl/Tk texts before PyInstaller, and every pinned
  runtime package installed here ships a license text.

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
    # Registered before it runs, as a real import is: dataclasses resolves a
    # class's string annotations through sys.modules.
    sys.modules[spec.name] = module
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
# app_entry.configure_os_trust_store — frozen-only, best-effort, logged
# --------------------------------------------------------------------------


class TestConfigureOsTrustStore:
    def test_not_frozen_is_a_no_op_that_imports_nothing(self, monkeypatch, app_entry):
        monkeypatch.delattr(sys, "frozen", raising=False)
        # A None entry makes ``import truststore`` raise, so touching it here
        # would surface as "unavailable" instead of "not-frozen".
        monkeypatch.setitem(sys.modules, "truststore", None)
        assert app_entry.configure_os_trust_store() == "not-frozen"

    def test_frozen_injects_into_ssl(self, monkeypatch, app_entry):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        calls: list[str] = []
        fake = types.ModuleType("truststore")
        fake.inject_into_ssl = lambda: calls.append("inject")  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "truststore", fake)
        assert app_entry.configure_os_trust_store() == "injected"
        assert calls == ["inject"]

    def test_frozen_without_truststore_logs_and_continues(self, monkeypatch, app_entry, caplog):
        import logging

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setitem(sys.modules, "truststore", None)  # ImportError on import
        with caplog.at_level(logging.WARNING, logger="spec_critic.app_entry"):
            assert app_entry.configure_os_trust_store() == "unavailable"
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("truststore" in m and "certifi" in m for m in messages), messages

    def test_frozen_injection_failure_logs_and_continues(self, monkeypatch, app_entry, caplog):
        import logging

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        fake = types.ModuleType("truststore")

        def boom():
            raise RuntimeError("no OpenSSL here")

        fake.inject_into_ssl = boom  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "truststore", fake)
        with caplog.at_level(logging.WARNING, logger="spec_critic.app_entry"):
            assert app_entry.configure_os_trust_store() == "unavailable"
        assert any("no OpenSSL here" in r.getMessage() for r in caplog.records)

    def test_main_configures_trust_store_second_before_any_src_import(self):
        # Cache first (pinned above), trust store second, and both strictly
        # before the first ``src`` import inside ``main`` — the updater's
        # urllib TLS contexts must already honour the OS store.
        tree = ast.parse((_PACKAGING / "app_entry.py").read_text(encoding="utf-8"))
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        second = main.body[1]
        assert isinstance(second, ast.Expr) and isinstance(second.value, ast.Call)
        assert isinstance(second.value.func, ast.Name)
        assert second.value.func.id == "configure_os_trust_store"
        first_src_import = next(
            i
            for i, node in enumerate(main.body)
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "src"
        )
        assert first_src_import > 1

    def test_truststore_is_pinned_in_the_runtime_lock(self):
        lock = (_REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
        assert re.search(r"^truststore==\d", lock, re.M), "truststore must stay pinned for the frozen build"


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

    def test_spec_writes_the_third_party_notices_after_the_analysis(self):
        text = self._spec_text()
        assert "from third_party_notices import NOTICES_FILENAME, write_third_party_notices" in text
        assert "from PyInstaller.utils.hooks.tcl_tk import tcltk_info" in text
        assert text.index("sys.path.insert(0, SPECPATH)") < text.index("from third_party_notices import")

        tree = ast.parse(text)
        order = {}
        call = None
        for index, node in enumerate(tree.body):
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                order[node.targets[0].id] = index
            # A top-level statement — not inside a try that could swallow the
            # failure a missing license text must cause.
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "write_third_party_notices"
            ):
                call, order["notices"] = node.value, index
        assert call is not None, "the spec never writes the third-party notices"
        # After the analysis it reads, before the slow archive/exe/collect steps.
        assert order["a"] < order["notices"] < order["pyz"] < order["exe"] < order["coll"]

        out, tables = call.args
        assert ast.unparse(out) == "os.path.join(DISTPATH, NOTICES_FILENAME)"
        # Every table of collected files — modules, runtime hooks, binaries, data.
        assert ast.unparse(tables) == "[a.pure, a.scripts, a.binaries, a.datas]"
        kwargs = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
        # The directories PyInstaller's tkinter hook bundles Tcl/Tk from.
        assert kwargs == {
            "tcl_data_dir": "tcltk_info.tcl_data_dir",
            "tk_data_dir": "tcltk_info.tk_data_dir",
        }


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

    def test_license_sources_are_checked_before_pyinstaller(self):
        text = self._text()
        i_install = text.index("name: Install app + PyInstaller")
        i_check = text.index("name: Check interpreter and Tcl/Tk license texts")
        i_build = text.index("name: Build one-folder app (PyInstaller)")
        assert i_install < i_check < i_build
        assert "run: python packaging/windows/third_party_notices.py" in text[i_check:i_build]
        # Requirement bumps change what is bundled, so they rebuild the notices.
        paths = text[text.index("paths:"):text.index("workflow_dispatch:")]
        assert '"packaging/windows/**"' in paths
        assert '"requirements.txt"' in paths

    def test_notices_are_uploaded_apart_from_the_installer(self):
        text = self._text()
        installer = text[text.index("name: Upload build artifacts"):text.index("name: Upload third-party notices")]
        # The publish job reads artifact/SpecCriticSetup.exe: adding a file from
        # outside dist/installer/ would move the artifact root and break it.
        assert "name: windows-installer" in installer
        assert "THIRD-PARTY-NOTICES" not in installer
        notices_step = text[text.index("name: Upload third-party notices"):text.index("  publish:")]
        assert "name: third-party-notices" in notices_step
        assert "path: dist/THIRD-PARTY-NOTICES.txt" in notices_step
        assert "if-no-files-found: error" in notices_step
        publish = text[text.index("  publish:"):]
        assert "assets=(artifact/SpecCriticSetup.exe artifact/latest.json)" in publish
        assert "name: windows-installer" in publish


# --------------------------------------------------------------------------
# installer.iss — the license is shown, must be accepted, and ships installed
# --------------------------------------------------------------------------

_INSTALLER = _PACKAGING / "installer.iss"
_LICENSE = _REPO_ROOT / "LICENSE"
_ABOUT_DIALOG = _REPO_ROOT / "src" / "gui" / "about_usage_dialogs.py"


def _iss_sections(text: str) -> dict[str, list[str]]:
    """Inno Setup script → {lower-case section name: its non-comment lines}.

    Comment lines (``;``) and preprocessor lines (``#define`` …) are skipped.
    """
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        header = re.fullmatch(r"\[(\w+)\]", line)
        if header:
            current = sections.setdefault(header.group(1).lower(), [])
        elif current is not None:
            current.append(line)
    return sections


def _iss_entry_params(line: str) -> dict[str, str]:
    """``Source: "a"; DestDir: "{app}"; Flags: x`` → ``{"source": "a", ...}``."""
    params = {}
    for name, value in re.findall(r'(\w+):\s*("(?:[^"]|"")*"|[^;]*)', line):
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1].replace('""', '"')
        params[name.lower()] = value
    return params


def _iss_path(value: str) -> Path:
    """An .iss source path (relative to the script, backslashes) as a Path."""
    return _PACKAGING.joinpath(*value.split("\\")).resolve()


class TestInstallerLicense:
    """The installer shows LICENSE, the user must accept it, and it is installed.

    Inno Setup's License Agreement page preselects "I do not accept the
    agreement" and keeps Next disabled until the user selects "I accept the
    agreement" — so the page, and the acceptance, exist exactly when
    ``LicenseFile`` is set and no ``[Code]`` skips the page or ticks the box.
    """

    def _text(self) -> str:
        return _INSTALLER.read_text(encoding="utf-8")

    def _setup(self) -> dict[str, str]:
        directives = {}
        for line in _iss_sections(self._text())["setup"]:
            key, _, value = line.partition("=")
            directives[key.strip().lower()] = value.strip()
        return directives

    def test_setup_shows_the_repository_license(self):
        setup = self._setup()
        assert "licensefile" in setup, "installer.iss shows no License Agreement page"
        assert _iss_path(setup["licensefile"]) == _LICENSE.resolve()
        assert _LICENSE.is_file()

    def test_nothing_skips_the_page_or_accepts_for_the_user(self):
        text = self._text()
        # A [Code] ShouldSkipPage(wpLicense) or a script that checks
        # LicenseAcceptedRadio would remove the user's own acceptance.
        for name in ("wpLicense", "LicenseAcceptedRadio", "LicenseNotAcceptedRadio"):
            assert name not in text

    def test_license_is_installed_beside_the_app(self):
        entries = [_iss_entry_params(line) for line in _iss_sections(self._text())["files"]]
        installed = [
            e for e in entries
            if "source" in e and _iss_path(e["source"]) == _LICENSE.resolve()
        ]
        assert len(installed) == 1, "LICENSE is not installed with the app"
        entry = installed[0]
        assert entry["destdir"] == "{app}"
        assert entry["destname"] == "LICENSE.txt"

    def test_license_text_is_what_inno_can_display(self):
        data = _LICENSE.read_bytes()
        # Inno reads a Unicode .txt license only as UTF-8 (BOM optional) or
        # UTF-16LE; this file is UTF-8 without a BOM (ASCII today).
        assert not data.startswith((b"\xff\xfe", b"\xfe\xff"))
        text = data.decode("utf-8")
        # The license's Notices clause: the terms travel with their Required
        # Notice line, which is the first line of the file.
        assert text.splitlines()[0].startswith("Required Notice: Copyright")
        assert "# PolyForm Noncommercial License 1.0.0" in text

    def test_about_dialog_names_the_same_license(self):
        tree = ast.parse(_ABOUT_DIALOG.read_text(encoding="utf-8"))
        names = [
            node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_LICENSE_NAME" for t in node.targets)
            and isinstance(node.value, ast.Constant)
        ]
        assert names == ["PolyForm Noncommercial License 1.0.0"]
        assert f"# {names[0]}" in _LICENSE.read_text(encoding="utf-8")

    def test_license_changes_trigger_the_packaging_build(self):
        text = _WORKFLOW.read_text(encoding="utf-8")
        paths = text[text.index("paths:"):text.index("workflow_dispatch:")]
        assert '- "LICENSE"' in paths


# --------------------------------------------------------------------------
# third_party_notices — every bundled component's license text, or no build
# --------------------------------------------------------------------------

_NOTICES_SCRIPT = _PACKAGING / "third_party_notices.py"


@pytest.fixture
def notices():
    return _load_packaging_module("third_party_notices")


def _fake_dist(
    site: Path,
    name: str,
    version: str,
    *,
    files: dict[str, str | bytes] | None = None,
    meta_files: dict[str, str | bytes] | None = None,
    meta_lines: tuple[str, ...] = (),
) -> dict[str, Path]:
    """Install a minimal distribution into ``site`` (used as a sys.path entry).

    ``files`` are paths relative to ``site`` (package code, vendored notices);
    ``meta_files`` are relative to the ``.dist-info`` folder. Every file is
    listed in ``RECORD``, as a wheel install lists it. Returns the absolute
    path of each written file, keyed as given.
    """
    dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
    written: dict[str, str | bytes] = {
        f"{dist_info}/METADATA": "\n".join(
            ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}", *meta_lines]
        )
        + "\n"
    }
    keys = {}
    for rel, content in (meta_files or {}).items():
        written[f"{dist_info}/{rel}"] = content
        keys[rel] = f"{dist_info}/{rel}"
    for rel, content in (files or {}).items():
        written[rel] = content
        keys[rel] = rel
    for rel, content in written.items():
        path = site / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    rows = [f"{rel},," for rel in written] + [f"{dist_info}/RECORD,,"]
    (site / dist_info / "RECORD").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return {key: site / rel for key, rel in keys.items()}


def _dists(site: Path) -> list:
    import importlib.metadata

    return list(importlib.metadata.distributions(path=[str(site)]))


def _dist(site: Path, name: str):
    [dist] = [d for d in _dists(site) if d.metadata["Name"] == name]
    return dist


# Tcl 8.6.12's and Tk 8.6.12's license.terms (the copies CPython's Windows
# build uses), abridged between the opening and closing sentences the helper
# anchors on, with the CRLF endings and line wrapping those files have.
_TCL_TERMS = (
    "This software is copyrighted by the Regents of the University of\r\n"
    "California, Sun Microsystems, Inc., Scriptics Corporation, ActiveState\r\n"
    "Corporation and other parties.  The following terms apply to all files\r\n"
    "associated with the software unless explicitly disclaimed in\r\n"
    "individual files.\r\n"
    "\r\n"
    "The authors hereby grant permission to use, copy, modify, distribute,\r\n"
    "and license this software and its documentation for any purpose.\r\n"
    "\r\n"
    "GOVERNMENT USE: ... 252.227-7014 (b) (3) of DFARs.  Notwithstanding the\r\n"
    "foregoing, the authors grant the U.S. Government and others acting in its\r\n"
    "behalf permission to use and distribute the software in accordance with the\r\n"
    "terms specified in this license.\r\n"
)
_TK_TERMS = (
    "This software is copyrighted by the Regents of the University of\r\n"
    "California, Sun Microsystems, Inc., Scriptics Corporation, ActiveState\r\n"
    "Corporation, Apple Inc. and other parties.  The following terms apply to\r\n"
    "all files associated with the software unless explicitly disclaimed in\r\n"
    "individual files.\r\n"
    "\r\n"
    "GOVERNMENT USE: ... 252.227-7013 (b) (3) of DFARs.  Notwithstanding the\r\n"
    "foregoing, the authors grant the U.S. Government and others acting in its\r\n"
    "behalf permission to use and distribute the software in accordance with the\r\n"
    "terms specified in this license.\r\n"
)
_PSF = (
    "A. HISTORY OF THE SOFTWARE\r\n"
    "==========================\r\n"
    "\r\n"
    "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\r\n"
    "--------------------------------------------\r\n"
    "\r\n"
    "1. This LICENSE AGREEMENT is between the Python Software Foundation.\r\n"
)


def _python_org_install(root: Path) -> dict[str, Path]:
    """The layout of python.org's Windows install (CPython 3.11.9).

    ``LICENSE.txt`` is the PSF license with the bundled libraries' terms
    appended — Tcl's, then Tk's (``PCbuild/regen.targets``) — and
    ``tcl\\tk8.6`` carries ``license.terms`` while ``tcl\\tcl8.6`` has none.
    """
    base = root / "Python311"
    tcl = base / "tcl" / "tcl8.6"
    tk = base / "tcl" / "tk8.6"
    tcl.mkdir(parents=True)
    tk.mkdir(parents=True)
    (tcl / "init.tcl").write_text("# init\n", encoding="utf-8")
    (tk / "license.terms").write_bytes(_TK_TERMS.encode())
    license_txt = base / "LICENSE.txt"
    license_txt.write_bytes(
        (_PSF + "\r\nThis program is linked with and uses Microsoft Distributable Code.\r\n"
         + "\r\n" + _TCL_TERMS + "\r\n" + _TK_TERMS).encode()
    )
    return {"base": base, "license": license_txt, "tcl": tcl, "tk": tk}


def _notices_kwargs(install: dict[str, Path], site: Path) -> dict:
    return {
        "tcl_data_dir": install["tcl"],
        "tk_data_dir": install["tk"],
        "interpreter_license": install["license"],
        "python_version": "3.11.9",
        "distributions": _dists(site),
        "overrides_dir": None,
    }


def _mit(holder: str) -> str:
    return f"MIT License\n\nCopyright (c) {holder}\n\nPermission is hereby granted, free of charge.\n"


class TestBundledDistributions:
    """A distribution is bundled when the analysis collected a file it owns."""

    def test_owner_of_a_collected_file_is_listed_and_others_are_not(self, tmp_path, notices):
        site = tmp_path / "site"
        shipped = _fake_dist(site, "shipped", "1.0", files={"shipped/__init__.py": ""})
        _fake_dist(site, "installed-only", "2.0", files={"installed_only/__init__.py": ""})
        _fake_dist(site, "pyinstaller", "6.11.1")

        dists = notices.bundled_distributions(
            [str(shipped["shipped/__init__.py"])], _dists(site)
        )
        # Build-time-only packages (altgraph, pefile, pip ...) are installed but
        # never bundled; they must not be listed. PyInstaller always is.
        assert [d.metadata["Name"] for d in dists] == ["pyinstaller", "shipped"]

    def test_pyinstaller_is_always_listed_and_must_be_installed(self, tmp_path, notices):
        site = tmp_path / "site"
        _fake_dist(site, "shipped", "1.0", files={"shipped/__init__.py": ""})
        with pytest.raises(notices.NoticesError, match="pyinstaller is not installed"):
            notices.bundled_distributions([], _dists(site))

    def test_spec_critic_itself_is_never_listed(self, tmp_path, notices):
        site = tmp_path / "site"
        own = _fake_dist(site, "spec-critic", "3.10.0", files={"src/__init__.py": ""})
        _fake_dist(site, "pyinstaller", "6.11.1")
        dists = notices.bundled_distributions([str(own["src/__init__.py"])], _dists(site))
        assert [d.metadata["Name"] for d in dists] == ["pyinstaller"]

    def test_paths_are_normalized_and_the_first_copy_of_a_name_wins(self, tmp_path, notices):
        first, second = tmp_path / "first", tmp_path / "second"
        _fake_dist(first, "Shared_Name", "1.0", files={"shared/a.py": ""})
        _fake_dist(second, "shared-name", "9.9", files={"shared/b.py": ""})
        _fake_dist(first, "pyinstaller", "6.11.1")
        roundabout = first / "shared" / ".." / "shared" / "a.py"
        dists = notices.bundled_distributions(
            [str(roundabout), str(second / "shared" / "b.py")], _dists(first) + _dists(second)
        )
        assert [(d.metadata["Name"], d.version) for d in dists] == [
            ("pyinstaller", "6.11.1"),
            ("Shared_Name", "1.0"),
        ]

    def test_source_paths_reads_every_table_and_skips_entries_without_one(self, notices):
        pure = [("mod", "/x/mod.py", "PYMODULE"), ("ns", None, "PYMODULE")]
        binaries = [("a.pyd", "/x/a.pyd", "EXTENSION"), ("short",)]
        datas = [("d/f", "", "DATA"), ("d/g", "/x/g", "DATA")]
        assert notices.source_paths([pure, binaries, datas]) == ["/x/mod.py", "/x/a.pyd", "/x/g"]


class TestDistributionLicenseTexts:
    def test_declared_license_files_are_read_in_both_layouts(self, tmp_path, notices):
        site = tmp_path / "site"
        # Metadata 2.4 keeps declared files under .dist-info/licenses/ ...
        _fake_dist(site, "modern", "1.0", meta_files={"licenses/LICENSE": _mit("Modern")},
                   meta_lines=("License-File: LICENSE",))
        # ... older setuptools wheels directly in .dist-info.
        _fake_dist(site, "older", "1.0", meta_files={"COPYING.txt": _mit("Older")},
                   meta_lines=("License-File: COPYING.txt",))
        modern = notices.distribution_license_texts(_dist(site, "modern"), overrides_dir=None)
        older = notices.distribution_license_texts(_dist(site, "older"), overrides_dir=None)
        assert [(t.source, t.text) for t in modern] == [
            ("modern-1.0.dist-info/licenses/LICENSE", _mit("Modern").rstrip())
        ]
        assert [t.source for t in older] == ["older-1.0.dist-info/COPYING.txt"]

    def test_undeclared_files_then_vendored_notices_follow_the_declared_ones(self, tmp_path, notices):
        site = tmp_path / "site"
        _fake_dist(
            site, "pkg", "1.0",
            meta_lines=("License-File: LICENSE.MIT",),
            meta_files={
                "licenses/LICENSE.MIT": _mit("Pkg"),
                # Present but not declared (anthropic 1.7.0, pywin32-ctypes 0.2.3).
                "licenses/NOTICE": "Pkg notice\n",
            },
            files={
                "pkg/__init__.py": "",
                # Third-party code vendored inside the package.
                "pkg/_vendor/lib/LICENSE": "Apache License 2.0 for lib\n",
            },
        )
        texts = notices.distribution_license_texts(_dist(site, "pkg"), overrides_dir=None)
        assert [t.source for t in texts] == [
            "pkg-1.0.dist-info/licenses/LICENSE.MIT",
            "pkg-1.0.dist-info/licenses/NOTICE",
            "pkg/_vendor/lib/LICENSE",
        ]

    def test_a_module_named_license_is_code_not_a_text(self, tmp_path, notices):
        site = tmp_path / "site"
        _fake_dist(site, "pkg", "1.0", files={"pkg/license.py": "X = 1\n", "pkg/licenses.pyc": b"\0"})
        assert notices.distribution_license_texts(_dist(site, "pkg"), overrides_dir=None) == ()

    def test_blank_files_do_not_count_and_non_utf8_text_is_kept(self, tmp_path, notices):
        site = tmp_path / "site"
        _fake_dist(site, "blank", "1.0", meta_files={"LICENSE": "  \r\n\n"})
        _fake_dist(site, "latin", "1.0", meta_files={"LICENSE": "Copyright \xa9 J\xfcrgen\r\n".encode("latin-1")})
        assert notices.distribution_license_texts(_dist(site, "blank"), overrides_dir=None) == ()
        [text] = notices.distribution_license_texts(_dist(site, "latin"), overrides_dir=None)
        assert text.text == "Copyright \xa9 J\xfcrgen"

    def test_full_text_license_field_counts_only_when_no_file_does(self, tmp_path, notices):
        site = tmp_path / "site"
        # tiktoken 0.12.0 publishes its whole MIT text in the License field.
        field = ("License: MIT License", "        ", "        Copyright (c) 2022 OpenAI",
                 "        Permission is hereby granted, free of charge.")
        _fake_dist(site, "field-only", "1.0", meta_lines=field)
        _fake_dist(site, "short-field", "1.0", meta_lines=("License: MIT",))
        _fake_dist(site, "both", "1.0", meta_lines=field, meta_files={"LICENSE": _mit("Both")})

        [text] = notices.distribution_license_texts(_dist(site, "field-only"), overrides_dir=None)
        assert text.source == "the License field of its package metadata"
        assert "Copyright (c) 2022 OpenAI" in text.text
        assert notices.distribution_license_texts(_dist(site, "short-field"), overrides_dir=None) == ()
        [text] = notices.distribution_license_texts(_dist(site, "both"), overrides_dir=None)
        assert text.source == "both-1.0.dist-info/LICENSE"

    def test_override_file_is_used_only_for_its_exact_version(self, tmp_path, notices):
        site, overrides = tmp_path / "site", tmp_path / "overrides"
        _fake_dist(site, "No_License", "1.2.3")
        overrides.mkdir()
        (overrides / "no-license-1.2.3.txt").write_text(_mit("Upstream"), encoding="utf-8")
        [text] = notices.distribution_license_texts(_dist(site, "No_License"), overrides_dir=overrides)
        assert "third_party_licenses/no-license-1.2.3.txt" in text.source
        assert "Copyright (c) Upstream" in text.text

        # An upgrade needs a text checked against the new version.
        (overrides / "no-license-1.2.3.txt").rename(overrides / "no-license-1.2.2.txt")
        assert notices.distribution_license_texts(_dist(site, "No_License"), overrides_dir=overrides) == ()

    def test_license_identifier_precedence(self, tmp_path, notices):
        site = tmp_path / "site"
        _fake_dist(site, "expr", "1", meta_lines=("License-Expression: MIT OR Apache-2.0", "License: BSD"))
        _fake_dist(site, "field", "1", meta_lines=("License: MPL-2.0",))
        _fake_dist(site, "classifier", "1", meta_lines=(
            "License: UNKNOWN",
            "Classifier: License :: OSI Approved :: BSD License",
            "Classifier: Programming Language :: Python",
        ))
        _fake_dist(site, "nothing", "1")
        ident = {n: notices.license_identifier(_dist(site, n)) for n in ("expr", "field", "classifier", "nothing")}
        assert ident == {
            "expr": "MIT OR Apache-2.0",
            "field": "MPL-2.0",
            "classifier": "BSD License",
            "nothing": "not declared in its package metadata (see the text below)",
        }


class TestMissingLicenseFailsTheBuild:
    """The build stops, naming every gap, and leaves no notices file behind."""

    def test_every_package_without_a_text_is_named_and_nothing_is_written(self, tmp_path, notices):
        install = _python_org_install(tmp_path)
        site = tmp_path / "site"
        good = _fake_dist(site, "good", "1.0", files={"good/__init__.py": ""},
                          meta_files={"LICENSE": _mit("Good")})
        bad1 = _fake_dist(site, "bad-one", "0.1", files={"bad_one/__init__.py": ""})
        bad2 = _fake_dist(site, "bad-two", "0.2", files={"bad_two/core.pyd": b"\0"})
        _fake_dist(site, "pyinstaller", "6.11.1", meta_files={"COPYING.txt": "GPL with exception\n"})
        tables = [
            [("good", str(good["good/__init__.py"]), "PYMODULE"),
             ("bad_one", str(bad1["bad_one/__init__.py"]), "PYMODULE")],
            [("bad_two/core.pyd", str(bad2["bad_two/core.pyd"]), "EXTENSION")],
        ]
        out = tmp_path / "dist" / notices.NOTICES_FILENAME
        out.parent.mkdir()
        out.write_text("notices from an earlier build", encoding="utf-8")

        with pytest.raises(notices.NoticesError) as excinfo:
            notices.write_third_party_notices(out, tables, **_notices_kwargs(install, site))

        message = str(excinfo.value)
        assert "bad-one 0.1 is bundled but ships no license text" in message
        assert "bad-two 0.2 is bundled but ships no license text" in message
        assert "good" not in message
        assert "<name>-<version>.txt" in message  # the remedy
        assert len(excinfo.value.problems) == 2
        # A stale file would let the installer ship the previous build's list.
        assert not out.exists()

    def test_interpreter_tcl_tk_and_package_gaps_are_reported_together(self, tmp_path, notices):
        site = tmp_path / "site"
        pkg = _fake_dist(site, "bad", "1", files={"bad/__init__.py": ""})
        _fake_dist(site, "pyinstaller", "6.11.1", meta_files={"COPYING.txt": "GPL\n"})
        empty = tmp_path / "empty"
        (empty / "tcl8.6").mkdir(parents=True)
        (empty / "tk8.6").mkdir()
        with pytest.raises(notices.NoticesError) as excinfo:
            notices.build_notices(
                [[("bad", str(pkg["bad/__init__.py"]), "PYMODULE")]],
                tcl_data_dir=empty / "tcl8.6",
                tk_data_dir=empty / "tk8.6",
                interpreter_license=empty / "LICENSE.txt",
                distributions=_dists(site),
                overrides_dir=None,
            )
        problems = excinfo.value.problems
        assert any("empty or unreadable" in p for p in problems)
        assert any(p.startswith("the Tcl license terms were found neither") for p in problems)
        assert any(p.startswith("the Tk license terms were found neither") for p in problems)
        assert any(p.startswith("bad 1 is bundled") for p in problems)


class TestInterpreterAndTclTk:
    def test_python_org_windows_layout(self, tmp_path, notices):
        install = _python_org_install(tmp_path)
        python, tcl, tk = notices.base_components(
            tcl_data_dir=install["tcl"],
            tk_data_dir=install["tk"],
            interpreter_license=install["license"],
            python_version="3.11.9",
        )
        assert (python.title, python.license) == ("Python 3.11.9", "PSF-2.0")
        # The interpreter's license is reproduced whole: on python.org's
        # Windows build it also carries the terms of the libraries it ships.
        assert "Microsoft Distributable Code" in python.texts[0].text
        assert "\r" not in python.texts[0].text

        # tcl8.6 has no license file of its own; its terms come out of the
        # interpreter's LICENSE.txt — Tcl's block, not Tk's.
        assert (tcl.title, tcl.license) == ("Tcl 8.6", "TCL (the Tcl/Tk license)")
        [tcl_text] = tcl.texts
        assert "interpreter's LICENSE.txt" in tcl_text.source
        assert tcl_text.text.startswith("This software is copyrighted by the Regents")
        assert "ActiveState\nCorporation and other parties." in tcl_text.text
        assert "Apple Inc." not in tcl_text.text
        assert tcl_text.text.endswith("terms specified in this license.")

        # tk8.6 ships license.terms, which PyInstaller bundles with it.
        [tk_text] = tk.texts
        assert tk.title == "Tk 8.6"
        assert tk_text.source == "license.terms in the bundled tk8.6 library directory"
        assert "Apple Inc. and other parties." in tk_text.text

    def test_a_tcl_license_file_is_preferred_when_present(self, tmp_path, notices):
        install = _python_org_install(tmp_path)
        (install["tcl"] / "license.terms").write_text("Tcl terms from the file\n", encoding="utf-8")
        _, tcl, _ = notices.base_components(
            tcl_data_dir=install["tcl"], tk_data_dir=install["tk"],
            interpreter_license=install["license"],
        )
        assert tcl.texts == (
            notices.LicenseText("license.terms in the bundled tcl8.6 library directory",
                                "Tcl terms from the file"),
        )

    def test_a_license_that_is_not_pythons_is_refused(self, tmp_path, notices):
        install = _python_org_install(tmp_path)
        install["license"].write_text("MIT License\n\nSomething else entirely.\n", encoding="utf-8")
        with pytest.raises(notices.NoticesError) as excinfo:
            notices.base_components(tcl_data_dir=install["tcl"], tk_data_dir=install["tk"],
                                    interpreter_license=install["license"])
        assert any("is not the Python license" in p for p in excinfo.value.problems)
        # Without Python's license there is nowhere to find Tcl's terms either.
        assert any(p.startswith("the Tcl license terms") for p in excinfo.value.problems)

    def test_an_unknown_tcl_tk_directory_fails(self, tmp_path, notices):
        install = _python_org_install(tmp_path)
        with pytest.raises(notices.NoticesError) as excinfo:
            notices.base_components(tcl_data_dir=None, tk_data_dir=None,
                                    interpreter_license=install["license"])
        assert [p.split(" library directory")[0] for p in excinfo.value.problems] == ["the Tcl", "the Tk"]

    def test_interpreter_license_is_looked_up_in_the_base_install(self, tmp_path, notices):
        base, stdlib = tmp_path / "base", tmp_path / "lib" / "python3.11"
        base.mkdir()
        stdlib.mkdir(parents=True)
        (stdlib / "LICENSE.txt").write_text(_PSF, encoding="utf-8")
        # POSIX builds keep it with the standard library ...
        assert notices.interpreter_license_path(base, stdlib) == stdlib / "LICENSE.txt"
        # ... python.org's Windows install in its root, which wins.
        (base / "LICENSE.txt").write_text(_PSF, encoding="utf-8")
        assert notices.interpreter_license_path(base, stdlib) == base / "LICENSE.txt"
        with pytest.raises(notices.NoticesError, match="interpreter's license file was not found"):
            notices.interpreter_license_path(tmp_path / "none", tmp_path / "none")

    def test_cli_main_checks_the_environment_before_the_build(self, tmp_path, notices, monkeypatch, capsys):
        install = _python_org_install(tmp_path)
        fake_tcl_tk = types.ModuleType("PyInstaller.utils.hooks.tcl_tk")
        fake_tcl_tk.tcltk_info = types.SimpleNamespace(
            tcl_data_dir=str(install["tcl"]), tk_data_dir=str(install["tk"])
        )
        for name in ("PyInstaller", "PyInstaller.utils", "PyInstaller.utils.hooks"):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks.tcl_tk", fake_tcl_tk)
        monkeypatch.setattr(notices.sys, "base_prefix", str(install["base"]))

        assert notices.main([]) == 0
        out = capsys.readouterr().out
        assert "Tcl 8.6: the Tcl license terms in the interpreter's LICENSE.txt" in out
        assert "Tk 8.6: license.terms in the bundled tk8.6 library directory" in out

        (install["tk"] / "license.terms").unlink()
        install["license"].write_bytes(_PSF.encode())
        assert notices.main([]) == 1
        err = capsys.readouterr().err
        assert err.startswith("ERROR:")
        assert "the Tk license terms were found neither" in err


class TestNoticesDocument:
    def _build(self, tmp_path, notices):
        install = _python_org_install(tmp_path)
        site = tmp_path / "site"
        certifi = _fake_dist(site, "certifi", "2026.4.22", files={"certifi/cacert.pem": "pem"},
                             meta_lines=("License: MPL-2.0", "License-File: LICENSE"),
                             meta_files={"licenses/LICENSE": "Mozilla Public License Version 2.0\n© text\n"})
        anyio = _fake_dist(site, "anyio", "4.14.2", files={"anyio/__init__.py": ""},
                           meta_lines=("License-Expression: MIT",),
                           meta_files={"licenses/LICENSE": _mit("Alex Gronholm")})
        _fake_dist(site, "pyinstaller", "6.11.1", meta_files={"COPYING.txt": "GPLv2 with bootloader exception\n"},
                   meta_lines=("License: GPLv2-or-later with a special exception",))
        _fake_dist(site, "altgraph", "0.17.5", files={"altgraph/__init__.py": ""},
                   meta_files={"LICENSE": _mit("altgraph")})
        tables = [
            [("anyio", str(anyio["anyio/__init__.py"]), "PYMODULE")],
            [],
            [("certifi/cacert.pem", str(certifi["certifi/cacert.pem"]), "DATA")],
        ]
        return tables, _notices_kwargs(install, site)

    def test_written_file_lists_and_reproduces_every_component(self, tmp_path, notices):
        tables, kwargs = self._build(tmp_path, notices)
        out = notices.write_third_party_notices(tmp_path / "dist" / notices.NOTICES_FILENAME, tables, **kwargs)
        data = out.read_bytes()
        # UTF-8 with a BOM and CRLF, so every Notepad version shows it right.
        assert data.startswith(b"\xef\xbb\xbf")
        assert b"\n" not in data.replace(b"\r\n", b"")
        text = data.decode("utf-8-sig").replace("\r\n", "\n")

        contents = text[text.index("Components\n"):text.index("=" * 78)]
        assert contents.splitlines()[2:] == [
            "  Python 3.11.9 - PSF-2.0",
            "  Tcl 8.6 - TCL (the Tcl/Tk license)",
            "  Tk 8.6 - TCL (the Tcl/Tk license)",
            "  anyio 4.14.2 - MIT",
            "  certifi 2026.4.22 - MPL-2.0",
            "  pyinstaller 6.11.1 - GPLv2-or-later with a special exception",
            "",
            "",
        ]
        # Installed for the build but not bundled.
        assert "altgraph" not in text
        assert "Spec Critic's\nown license is in LICENSE.txt" in text
        for heading, body in (
            ("anyio 4.14.2\nLicense: MIT", "Copyright (c) Alex Gronholm"),
            ("certifi 2026.4.22\nLicense: MPL-2.0", "Mozilla Public License Version 2.0\n© text"),
            ("pyinstaller 6.11.1\nLicense: GPLv2", "GPLv2 with bootloader exception"),
            ("Python 3.11.9 (the bundled interpreter)\nLicense: PSF-2.0", "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2"),
            ("Tcl 8.6 (bundled with tkinter)", "ActiveState\nCorporation and other parties."),
            ("Tk 8.6 (bundled with tkinter)", "Apple Inc. and other parties."),
        ):
            assert heading in text
            assert body in text[text.index(heading):]
        assert "--- certifi-2026.4.22.dist-info/licenses/LICENSE ---" in text

    def test_output_is_deterministic(self, tmp_path, notices):
        tables, kwargs = self._build(tmp_path, notices)
        first, _ = notices.build_notices(tables, **kwargs)
        reordered = [list(reversed(t)) for t in reversed(tables)]
        kwargs["distributions"] = list(reversed(kwargs["distributions"]))
        second, _ = notices.build_notices(reordered, **kwargs)
        assert first == second


class TestRuntimeLockShipsLicenseTexts:
    """Every pinned runtime package installed here ships a license text.

    The Windows build fails when a bundled package ships none; this finds it on
    the pull request that bumps the pin, in the ordinary test run.
    """

    def test_every_installed_pin_has_a_license_text(self, notices):
        import importlib.metadata

        pins = []
        for line in (_REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if "==" in line:
                pins.append(line.split("==", 1)[0].strip())
        assert len(pins) > 20

        checked, missing = [], []
        for name in pins:
            try:
                dist = importlib.metadata.distribution(name)
            except importlib.metadata.PackageNotFoundError:
                continue  # e.g. a local run without the runtime lock installed
            checked.append(name)
            if not notices.distribution_license_texts(dist, overrides_dir=notices.LICENSE_OVERRIDES_DIR):
                missing.append(f"{name} {dist.version}")
        if not checked:
            pytest.skip("the runtime lock is not installed in this environment")
        assert missing == [], f"packages that ship no license text: {missing}"


class TestInstallerNotices:
    def test_notices_are_installed_beside_the_app(self, notices):
        entries = [
            _iss_entry_params(line)
            for line in _iss_sections(_INSTALLER.read_text(encoding="utf-8"))["files"]
        ]
        notices_file = (_REPO_ROOT / "dist" / notices.NOTICES_FILENAME).resolve()
        installed = [e for e in entries if "source" in e and _iss_path(e["source"]) == notices_file]
        assert len(installed) == 1, "THIRD-PARTY-NOTICES.txt is not installed with the app"
        entry = installed[0]
        assert entry["destdir"] == "{app}"
        assert "destname" not in entry  # installed under its own name
        # ISCC must stop when the file is missing, never skip it.
        assert "skipifsourcedoesntexist" not in entry.get("flags", "").lower()
