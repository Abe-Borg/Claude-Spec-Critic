"""The suite must collect on a host without ``tkinter`` — keep the two guards in sync.

Two mechanisms keep GUI-importing test modules from breaking collection when
the system Tk package is missing:

1. ``conftest._GUI_DEPENDENT_TESTS`` — ``pytest_ignore_collect`` skips the
   listed files outright when ``tkinter`` cannot be found.
2. A top-of-module ``pytest.importorskip("tkinter")`` (and ``"customtkinter"``
   where the module needs it) in the test file itself.

The list once named two files that no longer existed while missing four that
did import GUI code, so this meta-test derives the truth dynamically: it
collects every test module with a module-level ``src.gui`` / ``tkinter`` /
``customtkinter`` / ``tkinterdnd2`` import (plus every file the conftest list
names) and imports each one in ONE subprocess with those modules hidden
(``sys.modules[name] = None`` makes ``import name`` raise). Each candidate
lands in one of three buckets:

* ``ok``        — imports fine without Tk (a tkinter-free ``src.gui`` helper);
* ``skipped``   — its own ``importorskip`` fired before the GUI import;
* ``unguarded`` — the GUI import raised: it MUST be in the conftest list.

Every conftest entry must in turn be a real file that actually needs Tk
(``skipped`` or ``unguarded``), so a stale or misspelled entry fails here.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent
_CONFTEST = _TESTS_DIR / "conftest.py"

_GUI_ROOTS = {"tkinter", "customtkinter", "tkinterdnd2"}

# Runs in a fresh interpreter: hide Tk, import each candidate, report buckets.
_PROBE = r"""
import importlib, json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real-do-not-use")
import pytest  # so importorskip raises pytest's Skipped outcome
for name in ("tkinter", "_tkinter", "customtkinter", "tkinterdnd2"):
    sys.modules[name] = None
out = {}
for mod in sys.argv[2:]:
    try:
        importlib.import_module(mod)
        out[mod] = "ok"
    except pytest.skip.Exception:
        out[mod] = "skipped"
    except ImportError as exc:
        out[mod] = "unguarded: " + str(exc)[:120]
    except BaseException as exc:  # noqa: BLE001 - reported, then asserted on
        out[mod] = "error: " + type(exc).__name__ + ": " + str(exc)[:120]
print(json.dumps(out))
"""


def _is_gui_import(node: ast.stmt) -> bool:
    if isinstance(node, ast.Import):
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in _GUI_ROOTS or alias.name == "src.gui" or alias.name.startswith("src.gui."):
                return True
        return False
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if module.split(".")[0] in _GUI_ROOTS:
            return True
        if module == "src.gui" or module.startswith("src.gui."):
            return True
        if module == "src" and any(alias.name == "gui" for alias in node.names):
            return True
    return False


def _imported_test_modules(tree: ast.Module) -> set[str]:
    """Sibling test files this module imports at module level (``tests.test_x`` -> ``test_x.py``)."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("tests.test_"):
                names.add(module.split(".", 1)[1].split(".")[0] + ".py")
            elif module == "tests":
                names.update(a.name + ".py" for a in node.names if a.name.startswith("test_"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("tests.test_"):
                    names.add(alias.name.split(".", 1)[1].split(".")[0] + ".py")
    return names


def scan_test_modules() -> tuple[set[str], dict[str, set[str]]]:
    """``(files with a module-level GUI import, {file: sibling test files it imports})``."""
    gui_importers: set[str] = set()
    imports: dict[str, set[str]] = {}
    for path in sorted(_TESTS_DIR.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(_is_gui_import(node) for node in tree.body):
            gui_importers.add(path.name)
        imports[path.name] = _imported_test_modules(tree)
    return gui_importers, imports


def module_level_gui_importers() -> set[str]:
    """Test files (by name) with a module-level GUI import — static candidates."""
    return scan_test_modules()[0]


def candidate_files(listed: set[str]) -> set[str]:
    """GUI importers, the conftest list, and — transitively — every test file that
    imports one of them at module level: ``from tests.test_program_pipeline import
    _result`` executes that module's imports too, so a GUI import there breaks
    the importer's collection unless a guard fires first."""
    gui_importers, imports = scan_test_modules()
    candidates = set(gui_importers) | set(listed)
    while True:
        more = {name for name, deps in imports.items() if deps & candidates} - candidates
        if not more:
            return candidates
        candidates |= more


def conftest_gui_dependent_tests() -> set[str]:
    """The ``_GUI_DEPENDENT_TESTS`` literal, read statically (no conftest re-execution)."""
    tree = ast.parse(_CONFTEST.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_GUI_DEPENDENT_TESTS" for t in node.targets
        ):
            value = node.value
            assert isinstance(value, ast.Set), "_GUI_DEPENDENT_TESTS must stay a literal set"
            names = set()
            for elt in value.elts:
                assert isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                names.add(elt.value)
            return names
    raise AssertionError("conftest.py no longer defines _GUI_DEPENDENT_TESTS")


def probe_without_tk(file_names: set[str]) -> dict[str, str]:
    """Import each test module with Tk hidden; ``{file name: bucket}``."""
    modules = [f"tests.{name[:-3]}" for name in sorted(file_names) if (_TESTS_DIR / name).is_file()]
    if not modules:
        return {}
    env = dict(os.environ)
    env.setdefault("ANTHROPIC_API_KEY", "test-key-not-real-do-not-use")
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(_REPO_ROOT), *modules],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env=env,
        timeout=300,
    )
    assert proc.returncode == 0, f"probe interpreter failed:\n{proc.stderr}"
    last_line = proc.stdout.strip().splitlines()[-1]
    raw = json.loads(last_line)
    return {f"{mod.split('.', 1)[1]}.py": bucket for mod, bucket in raw.items()}


def test_conftest_list_names_only_existing_files() -> None:
    listed = conftest_gui_dependent_tests()
    missing = sorted(name for name in listed if not (_TESTS_DIR / name).is_file())
    assert not missing, f"stale _GUI_DEPENDENT_TESTS entries (files do not exist): {missing}"


def test_gui_importers_are_guarded_and_the_conftest_list_is_exact() -> None:
    listed = conftest_gui_dependent_tests()
    candidates = candidate_files(listed)
    buckets = probe_without_tk(candidates)

    errors = {name: b for name, b in buckets.items() if b.startswith("error:")}
    assert not errors, f"unexpected failure importing with Tk hidden: {errors}"

    needs_tk = {name for name, b in buckets.items() if b != "ok"}
    unguarded = {name for name, b in buckets.items() if b.startswith("unguarded")}

    # (1) An unguarded GUI importer that the conftest list does not name would
    #     break collection on a Tk-less host.
    not_listed = sorted(unguarded - listed)
    assert not not_listed, (
        "test modules reach GUI code at module scope (directly, or through a "
        "`from tests.test_x import ...` of a GUI-importing sibling) without a "
        "top-of-module pytest.importorskip firing first AND are missing from "
        f"conftest._GUI_DEPENDENT_TESTS: {not_listed}"
    )
    # (2) Every listed file must really need Tk — a listed file that imports
    #     fine without it is a stale entry that silently skips real tests.
    stale = sorted(name for name in listed if name not in needs_tk)
    assert not stale, f"_GUI_DEPENDENT_TESTS entries that no longer need tkinter: {stale}"
    # A self-guarded module (``skipped``) is hermetic whether or not it is
    # listed — the contract is "importorskip at the top OR in the list", so a
    # new GUI test that guards itself is not forced into conftest.
    assert needs_tk == unguarded | {name for name, b in buckets.items() if b == "skipped"}


def test_known_gui_modules_carry_their_own_importorskip() -> None:
    """The files this task guarded stay self-guarding, independent of the list."""
    for name in (
        "test_program_pipeline.py",
        "test_program_routing.py",
        "test_activity_log_pump.py",
        "test_html_gui_hook.py",
    ):
        tree = ast.parse((_TESTS_DIR / name).read_text(encoding="utf-8"))
        first_gui_import = next(i for i, node in enumerate(tree.body) if _is_gui_import(node))
        skip_calls = [
            i
            for i, node in enumerate(tree.body)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "importorskip"
            and node.value.args
            and isinstance(node.value.args[0], ast.Constant)
            and node.value.args[0].value == "tkinter"
        ]
        assert skip_calls and skip_calls[0] < first_gui_import, (
            f"{name}: pytest.importorskip('tkinter') must precede the first GUI import"
        )
