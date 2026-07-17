"""Keep the two version literals in lockstep with the updater's grammar.

The Windows release pipeline requires the git tag to match BOTH
``pyproject.toml``'s ``project.version`` and ``src/__init__.py``'s
``__version__`` (see ``packaging/windows/check_release_version.py``). This
hermetic test runs on every push/PR (``tests.yml``) so a half-bumped version
is caught long before a tag is pushed — a drifted pair would ship an
installer stuck in a perpetual "update available" loop (the manifest carries
the tag's version while the installed app keeps reporting the stale
``__version__``).
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from src import __version__
from src.core import updates

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def test_version_literals_in_lockstep() -> None:
    assert _pyproject_version() == __version__


def test_version_matches_updater_grammar() -> None:
    # parse_version raises on anything outside MAJOR.MINOR.PATCH[rcN]; a
    # version the updater can't parse could never be compared to a manifest.
    updates.parse_version(__version__)
    updates.parse_version(_pyproject_version())
