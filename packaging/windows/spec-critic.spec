# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-folder build for the Spec Critic Windows desktop app.

Build (on Windows, from the repo root) with:

    pip install -r requirements.txt
    pip install -e . --no-deps
    pip install keyring pyinstaller
    pyinstaller packaging/windows/spec-critic.spec --noconfirm --clean

Output: ``dist/SpecCritic/`` (a folder containing ``SpecCritic.exe`` plus its
bundled interpreter and dependencies). ``packaging/windows/installer.iss``
wraps that folder into ``SpecCriticSetup.exe``.

The spec is exec'd by PyInstaller with globals like ``Analysis``/``PYZ``/``EXE``/
``COLLECT``/``SPECPATH`` injected — that is why linters flag "undefined name"
here; the file lives outside ``src``/``tests`` so no lint gate sees it.

One-folder (not one-file) is deliberate: it starts faster, updates more
reliably, and trips antivirus far less than a self-extracting one-file exe —
and the Inno Setup installer makes it a normal double-click "install" for the
user regardless.
"""
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

datas = []
binaries = []
hiddenimports = []

# customtkinter ships theme JSON assets; tkinterdnd2 ships the native tkdnd
# library — both must be collected or the app renders wrong / drag-and-drop
# fails to load at runtime.
for _pkg in ("customtkinter", "tkinterdnd2"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

# tiktoken discovers its encodings through the ``tiktoken_ext`` namespace
# package via dynamic import — a classic PyInstaller miss.
hiddenimports += collect_submodules("tiktoken_ext")
hiddenimports += ["tiktoken_ext.openai_public"]

# keyring resolves its backend (Windows Credential Manager) dynamically; bundle
# every backend plus the metadata it reads to enumerate them. keyring is an
# OPTIONAL dependency of the app (api_key_store falls back to the key file when
# it is absent), so a build without it still succeeds — it just ships without
# Credential Manager support.
try:
    hiddenimports += collect_submodules("keyring.backends")
    hiddenimports += ["keyring.backends.Windows"]
except Exception:
    pass

# Distribution metadata read at runtime. Including our own means
# ``importlib.metadata.version('spec-critic')`` keeps working in the frozen
# app, matching the source install.
for _dist in ("spec-critic", "anthropic", "keyring", "tiktoken"):
    try:
        datas += copy_metadata(_dist)
    except Exception:
        # A missing dist here is non-fatal — the app still runs; only the
        # metadata-derived version string would fall back.
        pass

# The app package itself (the package is literally named ``src``): its data
# files plus any dynamically imported submodules.
_d, _b, _h = collect_all("src")
datas += _d
binaries += _b
hiddenimports += _h

# The bundled HTML trace viewer is resolved at runtime relative to
# src/gui/gui.py's __file__ (src/tracing/viewer/trace_viewer.html), so it must
# land at that same relative path inside the bundle. collect_all("src") already
# picks it up; this explicit entry keeps the build correct even if the
# collection heuristics change (duplicates are deduped by PyInstaller).
_repo_root = os.path.dirname(os.path.dirname(SPECPATH))
datas += [(
    os.path.join(_repo_root, "src", "tracing", "viewer", "trace_viewer.html"),
    os.path.join("src", "tracing", "viewer"),
)]

a = Analysis(
    [os.path.join(SPECPATH, "app_entry.py")],
    pathex=[_repo_root],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Test-only / dev-only packages must never be pulled into the shipped app.
    excludes=["pytest", "_pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SpecCritic",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed GUI app — no console window behind it
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # drop an .ico here (icon="app.ico") once one exists
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SpecCritic",
)
