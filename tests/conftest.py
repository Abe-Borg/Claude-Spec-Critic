"""Shared pytest configuration for the Spec Critic test suite.

Keep tests hermetic by default.

- Tests must never need a real ``ANTHROPIC_API_KEY``. The reviewer/verifier
  client cache reads the env var lazily, so we set a sentinel value before
  collection. Any test that needs a real network call should opt in via
  ``@pytest.mark.network`` and be skipped unless ``ANTHROPIC_API_KEY`` is set.
- ``fake_anthropic`` is exposed as a top-level fixture so request-shape and
  parser tests can build response objects without instantiating the real SDK.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _tkinter_available() -> bool:
    return importlib.util.find_spec("tkinter") is not None


# Skip GUI-dependent test files at collection time when ``tkinter`` is missing
# (common in CI / containers without the python3-tk system package). These
# files import a ``src.gui`` module that pulls in ``tkinter`` / ``customtkinter``
# at module scope, so collection would fail outright otherwise. When tkinter is
# installed, they run normally. Each file also guards itself with a top-of-module
# ``pytest.importorskip`` where it can, so hermeticity does not depend on this
# list alone; ``tests/test_gui_import_hermeticity.py`` imports every candidate
# with tkinter hidden and fails when the two drift (a stale entry, a missing
# file, or a new unguarded GUI import that is not listed here).
_GUI_DEPENDENT_TESTS = {
    "test_activity_log_pump.py",
    "test_context_controller_background.py",
    "test_gui_close_and_logging.py",
    "test_html_gui_hook.py",
    "test_program_pipeline.py",
    "test_program_routing.py",
    "test_report_export_lifecycle.py",
    "test_review_complete_terminal_state.py",
    "test_trace_recorder_teardown.py",
}


def pytest_ignore_collect(collection_path, config):
    if not _tkinter_available() and collection_path.name in _GUI_DEPENDENT_TESTS:
        return True
    return None


def pytest_configure(config: pytest.Config) -> None:
    """Inject a placeholder API key so import-time helpers never raise.

    ``reviewer._get_api_key`` raises if ``ANTHROPIC_API_KEY`` is missing.
    The placeholder is obviously fake so any accidental real call will 401
    instead of silently charging a different account.
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
