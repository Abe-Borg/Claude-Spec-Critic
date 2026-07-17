"""Fail a release build unless the git tag matches BOTH version literals.

The shipped app reports ``src/__init__.py::__version__`` — that is the value
the updater compares against the manifest — while the wheel/sdist use
``pyproject.toml``'s ``project.version``. ``tests/test_release_metadata.py``
keeps the two in lockstep, but that test runs in ``tests.yml`` (push to
master / PRs), NOT on tag pushes, which only fire ``release.yml``. So the
release workflow calls this guard directly: a tag must never publish an
installer whose reported ``__version__`` drifts from the tag, which would
otherwise trap users in a perpetual "update available" loop (the manifest
version would be the tag but the installed app would keep reporting the stale
``__version__``).

Pure standard library (``tomllib`` ships with Python 3.11+), reads the files
without importing the package, so it runs before any ``pip install``.

Usage:  python packaging/windows/check_release_version.py --tag v3.1.0
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import sys
import tomllib

_ROOT = pathlib.Path(__file__).resolve().parents[2]


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


def check(tag: str, *, root: pathlib.Path = _ROOT) -> list[str]:
    """Return a list of mismatch messages (empty when the tag matches both)."""
    tag_version = tag[1:] if tag.startswith("v") else tag
    problems = []
    pyproject = pyproject_version(root)
    init = init_version(root)
    if pyproject != tag_version:
        problems.append(f"pyproject.toml version {pyproject!r} != tag {tag_version!r}")
    if init != tag_version:
        problems.append(f"__init__.py __version__ {init!r} != tag {tag_version!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guard tag == package version literals.")
    parser.add_argument("--tag", required=True, help="git tag, e.g. v3.1.0")
    args = parser.parse_args(argv)

    tag_version = args.tag[1:] if args.tag.startswith("v") else args.tag
    print(
        f"tag={tag_version} pyproject.toml={pyproject_version()} "
        f"__init__.py={init_version()}"
    )
    problems = check(args.tag)
    if problems:
        for problem in problems:
            print("ERROR:", problem, file=sys.stderr)
        print(
            "Bump BOTH version literals (pyproject.toml AND src/__init__.py) "
            "to match the tag, then re-tag.",
            file=sys.stderr,
        )
        return 1
    print("release version guard: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
