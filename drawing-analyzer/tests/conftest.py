"""Shared pytest configuration for the drawing-analyzer test suite.

Keep tests hermetic by default.

- Tests must never need a real ``ANTHROPIC_API_KEY``. The client cache reads the
  env var lazily, so we set a sentinel value before collection. Any test that
  needs a real network call should opt in via ``@pytest.mark.network`` and be
  skipped unless ``ANTHROPIC_API_KEY`` is set.
- ``fake_anthropic`` is exposed as a top-level fixture so request-shape and
  parser tests can build response objects without instantiating the real SDK.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# Put the repo root on sys.path so ``tests.fixtures.fake_anthropic`` imports.
# The package itself is importable via ``[tool.pytest.ini_options] pythonpath``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _tkinter_available() -> bool:
    return importlib.util.find_spec("tkinter") is not None


# Test files that import the GUI (customtkinter / tkinter) at module scope are
# skipped at collection time when ``tkinter`` is missing (common in CI without
# the python3-tk system package). None today — the GUI has no unit tests — but
# the hook stays so adding one is a one-line change here.
_GUI_DEPENDENT_TESTS: set[str] = set()


def pytest_ignore_collect(collection_path, config):
    if not _tkinter_available() and collection_path.name in _GUI_DEPENDENT_TESTS:
        return True
    return None


def pytest_configure(config: pytest.Config) -> None:
    """Inject a placeholder API key so import-time helpers never raise.

    ``client.get_client`` raises if ``ANTHROPIC_API_KEY`` is missing. The
    placeholder is obviously fake so any accidental real call will 401 instead
    of silently charging a different account.
    """
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real-do-not-use")


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip ``@pytest.mark.network`` tests unless a real API key is set."""
    real_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if real_key and real_key != "test-key-not-real-do-not-use":
        return
    skip_marker = pytest.mark.skip(reason="ANTHROPIC_API_KEY not set; skipping network test")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip_marker)


# ---------------------------------------------------------------------------
# Fake Anthropic response fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_anthropic():
    """Expose the ``fake_anthropic`` helper module as a fixture.

    Tests can ``request.getfixturevalue("fake_anthropic")`` or take
    ``fake_anthropic`` as an argument and use the builders directly.
    """
    from tests.fixtures import fake_anthropic as module

    return module
