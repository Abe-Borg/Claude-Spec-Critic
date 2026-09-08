"""Keep every version literal in lockstep, plus the dependency metadata honest.

The Windows release pipeline requires the git tag to match BOTH
``pyproject.toml``'s ``project.version`` and ``src/__init__.py``'s
``__version__`` (see ``packaging/windows/check_release_version.py``). This
hermetic test runs on every push/PR (``tests.yml``) so a half-bumped version
is caught long before a tag is pushed — a drifted pair would ship an
installer stuck in a perpetual "update available" loop (the manifest carries
the tag's version while the installed app keeps reporting the stale
``__version__``).

The same guard now reads the three documentation literals (README.md's
``**vX.Y.Z**`` headline, CLAUDE.md's title line and its
``# Package version (X.Y.Z)`` note) through ``check_docs``; the tests below
pin both that those docs agree with the package today and that the guard
actually rejects a drifted or missing literal.

The dependency-metadata tests pin the split between the runtime lock
(``requirements.txt`` — what the Windows build freezes) and the test chain
(``requirements-dev.txt``), and that every package ``src/`` imports directly
is declared in ``pyproject.toml`` and pinned in the runtime lock.
"""
from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path

import pytest

from src import __version__
from src.core import updates

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RELEASE_GUARD = _REPO_ROOT / "packaging" / "windows" / "check_release_version.py"


def _pyproject() -> dict:
    return tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _pyproject_version() -> str:
    return str(_pyproject()["project"]["version"])


def _load_release_guard():
    """Load the stdlib-only guard from its file path (it is a script, not part of ``src``)."""
    spec = importlib.util.spec_from_file_location("check_release_version_under_test", _RELEASE_GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _requirement_name(spec: str) -> str:
    """Normalized distribution name from a requirement line / pyproject entry."""
    name = re.split(r"[<>=!~;\[\s]", spec.strip(), maxsplit=1)[0]
    return name.lower().replace("_", "-")


def _pinned_names(path: Path) -> set[str]:
    names: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-r "):
            continue
        names.add(_requirement_name(line))
    return names


def test_version_literals_in_lockstep() -> None:
    assert _pyproject_version() == __version__


def test_version_matches_updater_grammar() -> None:
    # parse_version raises on anything outside MAJOR.MINOR.PATCH[rcN]; a
    # version the updater can't parse could never be compared to a manifest.
    updates.parse_version(__version__)
    updates.parse_version(_pyproject_version())


# --------------------------------------------------------------------------
# Documentation literals (README.md / CLAUDE.md) — read-only guard
# --------------------------------------------------------------------------


def test_doc_version_literals_in_lockstep() -> None:
    guard = _load_release_guard()
    found = guard.doc_versions(_REPO_ROOT)
    # Exactly the three documented literals, in a stable order.
    assert [(rel, label) for rel, label, _ in found] == [
        ("README.md", "**vX.Y.Z** headline"),
        ("CLAUDE.md", "'# CLAUDE.md — Spec Critic vX.Y.Z' title"),
        ("CLAUDE.md", "'# Package version (X.Y.Z)' source-layout note"),
    ]
    for rel, label, version in found:
        assert version == __version__, (
            f"{rel} {label} says {version!r} but the package is {__version__!r} — "
            "bump the doc line with the release"
        )


def test_release_guard_docs_accept_matching_tag() -> None:
    guard = _load_release_guard()
    assert guard.check_docs(f"v{__version__}") == []
    assert guard.check_docs(__version__) == []


def test_release_guard_docs_reject_mismatched_tag() -> None:
    guard = _load_release_guard()
    problems = guard.check_docs("v99.99.99")
    assert len(problems) == 3
    assert sum("README.md" in p for p in problems) == 1
    assert sum("CLAUDE.md" in p for p in problems) == 2
    assert all("'99.99.99'" in p for p in problems)


def test_release_guard_docs_report_a_missing_literal(tmp_path: Path) -> None:
    # A reworded doc line that no longer carries the literal must FAIL the
    # guard, not silently pass it: README has no headline, CLAUDE.md has the
    # title but not the package note.
    (tmp_path / "README.md").write_text("# Spec Critic\n\nno version here\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# CLAUDE.md — Spec Critic v1.2.3\n", encoding="utf-8")
    problems = _load_release_guard().check_docs("v1.2.3", root=tmp_path)
    assert len(problems) == 2
    assert all("could not find" in p for p in problems)
    assert any(p.startswith("README.md") for p in problems)
    assert any(p.startswith("CLAUDE.md") and "Package version" in p for p in problems)


def test_release_guard_docs_accept_a_matching_synthetic_tree(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("intro\n\n**v1.2.3** — blurb\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text(
        "# CLAUDE.md — Spec Critic v1.2.3\n\n├── __init__.py   # Package version (1.2.3)\n",
        encoding="utf-8",
    )
    assert _load_release_guard().check_docs("v1.2.3", root=tmp_path) == []
    assert len(_load_release_guard().check_docs("v1.2.4", root=tmp_path)) == 3


def test_release_guard_main_covers_package_and_docs(capsys: pytest.CaptureFixture[str]) -> None:
    guard = _load_release_guard()
    assert guard.main(["--tag", f"v{__version__}"]) == 0
    assert "release version guard: OK" in capsys.readouterr().out

    assert guard.main(["--tag", "v99.99.99"]) == 1
    err = capsys.readouterr().err
    # All five surfaces are named in the failure output.
    assert "pyproject.toml" in err
    assert "__init__.py" in err
    assert "README.md" in err
    assert "CLAUDE.md" in err
    assert err.count("ERROR:") == 5


def test_release_guard_package_check_stays_two_literals() -> None:
    # ``check`` is pinned elsewhere at exactly the two package literals; the
    # docs live in ``check_docs`` so that contract is unchanged.
    guard = _load_release_guard()
    assert guard.check(f"v{__version__}") == []
    assert len(guard.check("v99.99.99")) == 2


# --------------------------------------------------------------------------
# Dependency metadata — pyproject vs. the runtime / dev requirement files
# --------------------------------------------------------------------------

_TEST_CHAIN = {"pytest", "iniconfig", "pluggy", "pygments"}


def test_pyproject_declares_python_floor_and_directly_imported_packages() -> None:
    project = _pyproject()["project"]
    assert project["requires-python"] == ">=3.11"
    declared = {_requirement_name(d) for d in project["dependencies"]}
    # Imported directly by src/ (report_exporter -> lxml, api_key_store -> keyring).
    assert {"lxml", "keyring"} <= declared
    # The test chain never belongs in the runtime metadata.
    assert not (_TEST_CHAIN & declared)


def test_runtime_lock_pins_every_pyproject_dependency() -> None:
    declared = {_requirement_name(d) for d in _pyproject()["project"]["dependencies"]}
    runtime = _pinned_names(_REPO_ROOT / "requirements.txt")
    assert declared <= runtime, sorted(declared - runtime)
    # Transitive pins that are load-bearing for the frozen app stay in the lock.
    assert {"truststore", "requests", "lxml", "keyring"} <= runtime


def test_test_chain_lives_in_requirements_dev_only() -> None:
    dev_path = _REPO_ROOT / "requirements-dev.txt"
    assert dev_path.is_file()
    first_directive = next(
        line.strip()
        for line in dev_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )
    assert first_directive == "-r requirements.txt"
    runtime = _pinned_names(_REPO_ROOT / "requirements.txt")
    dev = _pinned_names(dev_path)
    assert _TEST_CHAIN <= dev
    assert not (_TEST_CHAIN & runtime), sorted(_TEST_CHAIN & runtime)


def test_workflows_install_the_right_requirement_files() -> None:
    tests_yml = (_REPO_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    release_yml = (_REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "pip install -r requirements-dev.txt" in tests_yml
    assert "pip check" in tests_yml
    # The Windows build freezes the runtime lock only — never the test chain.
    assert "pip install -r requirements.txt" in release_yml
    assert "pip install -r requirements-dev.txt" not in release_yml
    assert "pip check" in release_yml
    # keyring is pinned via requirements.txt now; no bare unpinned install remains.
    assert "pip install keyring" not in release_yml
