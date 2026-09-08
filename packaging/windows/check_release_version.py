"""Fail a release build unless the git tag matches EVERY version literal.

Two package literals decide behaviour: the shipped app reports
``src/__init__.py::__version__`` — the value the updater compares against the
manifest — while the wheel/sdist use ``pyproject.toml``'s ``project.version``.
``tests/test_release_metadata.py`` keeps them in lockstep, but that test runs
in ``tests.yml`` (push to master / PRs), NOT on tag pushes, which only fire
``release.yml``. So the release workflow calls this guard directly: a tag must
never publish an installer whose reported ``__version__`` drifts from the tag,
which would otherwise trap users in a perpetual "update available" loop (the
manifest version would be the tag but the installed app would keep reporting
the stale ``__version__``). ``check`` covers exactly those two.

Three documentation literals carry the same version and used to drift
unguarded: README.md's ``**vX.Y.Z**`` headline, CLAUDE.md's
``# CLAUDE.md — Spec Critic vX.Y.Z`` title, and CLAUDE.md's
``# Package version (X.Y.Z)`` source-layout note. ``check_docs`` reads them
(read-only — the guard never edits a doc) and ``main`` fails the release when
any of the five disagrees with the tag, so a bump can no longer ship docs
that describe the previous version.

Pure standard library (``tomllib`` ships with Python 3.11+), reads the files
without importing the package, so it runs before any ``pip install``.

Usage:  python packaging/windows/check_release_version.py --tag v3.1.0
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys
import tomllib

_ROOT = pathlib.Path(__file__).resolve().parents[2]

# The documentation literals, as ``(relative file, human label, pattern)``.
# Each pattern captures the version in group 1. Anchored tightly to the line
# shapes the docs actually use so an unrelated ``vN`` mention can never satisfy
# the guard by accident.
_README_HEADLINE_RE = re.compile(r"\*\*v(\d+\.\d+\.\d+(?:rc\d+)?)\*\*")
_CLAUDE_MD_TITLE_RE = re.compile(r"^# CLAUDE\.md — Spec Critic v(\S+)\s*$", re.MULTILINE)
_CLAUDE_MD_PACKAGE_NOTE_RE = re.compile(r"__init__\.py\s+# Package version \(([^)\s]+)\)")

DOC_VERSION_LITERALS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("README.md", "**vX.Y.Z** headline", _README_HEADLINE_RE),
    ("CLAUDE.md", "'# CLAUDE.md — Spec Critic vX.Y.Z' title", _CLAUDE_MD_TITLE_RE),
    ("CLAUDE.md", "'# Package version (X.Y.Z)' source-layout note", _CLAUDE_MD_PACKAGE_NOTE_RE),
)


def _tag_version(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def pyproject_version(root: pathlib.Path = _ROOT) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def init_version(root: pathlib.Path = _ROOT) -> str:
    src = (root / "src" / "__init__.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets
        ):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
    raise SystemExit("could not find a string __version__ in src/__init__.py")


def doc_versions(root: pathlib.Path = _ROOT) -> list[tuple[str, str, str | None]]:
    """``(file, label, version)`` for each documentation literal, in ``DOC_VERSION_LITERALS`` order.

    ``version`` is ``None`` when the file is missing or the literal cannot be
    found — reported as a problem by :func:`check_docs` rather than silently
    passing, so rewording a doc line out of the guard's reach is caught too.
    """
    found: list[tuple[str, str, str | None]] = []
    for rel, label, pattern in DOC_VERSION_LITERALS:
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except OSError:
            text = ""
        match = pattern.search(text)
        found.append((rel, label, match.group(1) if match else None))
    return found


def check(tag: str, *, root: pathlib.Path = _ROOT) -> list[str]:
    """Return mismatch messages for the two PACKAGE literals (empty when both match).

    Deliberately limited to ``pyproject.toml`` + ``src/__init__.py``; the
    documentation literals are :func:`check_docs` so each surface can be
    tested (and reported) on its own.
    """
    tag_version = _tag_version(tag)
    problems = []
    pyproject = pyproject_version(root)
    init = init_version(root)
    if pyproject != tag_version:
        problems.append(f"pyproject.toml version {pyproject!r} != tag {tag_version!r}")
    if init != tag_version:
        problems.append(f"__init__.py __version__ {init!r} != tag {tag_version!r}")
    return problems


def check_docs(tag: str, *, root: pathlib.Path = _ROOT) -> list[str]:
    """Return mismatch messages for the three DOCUMENTATION literals (empty when all match)."""
    tag_version = _tag_version(tag)
    problems = []
    for rel, label, version in doc_versions(root):
        if version is None:
            problems.append(f"{rel}: could not find the {label} version literal")
        elif version != tag_version:
            problems.append(f"{rel} {label} {version!r} != tag {tag_version!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Guard tag == package version literals (pyproject.toml, src/__init__.py) "
        "and the documented version (README.md, CLAUDE.md)."
    )
    parser.add_argument("--tag", required=True, help="git tag, e.g. v3.1.0")
    args = parser.parse_args(argv)

    tag_version = _tag_version(args.tag)
    print(
        f"tag={tag_version} pyproject.toml={pyproject_version()} "
        f"__init__.py={init_version()}"
    )
    for rel, label, version in doc_versions():
        print(f"{rel} {label}={version}")
    problems = check(args.tag) + check_docs(args.tag)
    if problems:
        for problem in problems:
            print("ERROR:", problem, file=sys.stderr)
        print(
            "Bump EVERY version literal to match the tag, then re-tag: "
            "pyproject.toml, src/__init__.py, README.md's **vX.Y.Z** headline, "
            "and CLAUDE.md's title line + '# Package version (X.Y.Z)' note.",
            file=sys.stderr,
        )
        return 1
    print("release version guard: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
