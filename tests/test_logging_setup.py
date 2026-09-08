"""``core.logging_setup``: the app's one file handler (B-20)."""
from __future__ import annotations

import logging

import pytest

from src.core import logging_setup as ls


@pytest.fixture(autouse=True)
def _clean_handler():
    ls.remove_file_logging()
    yield
    ls.remove_file_logging()


def test_attaches_rotating_handler_and_writes_warnings(tmp_path):
    target = tmp_path / "logs" / "app.log"
    assert ls.configure_file_logging(path=target) == target
    logging.getLogger("src.some.module").warning("unknown model id fallback")
    logging.getLogger("src.some.module").info("not written at WARNING")
    for h in logging.getLogger().handlers:
        h.flush()
    text = target.read_text(encoding="utf-8")
    assert "unknown model id fallback" in text
    assert "not written" not in text


def test_idempotent_second_call_returns_same_path_without_second_handler(tmp_path):
    target = tmp_path / "a.log"
    first = ls.configure_file_logging(path=target)
    before = len(logging.getLogger().handlers)
    second = ls.configure_file_logging(path=tmp_path / "ignored.log")
    assert first == second == target
    assert len(logging.getLogger().handlers) == before


def test_unwritable_directory_degrades_to_none(tmp_path, monkeypatch):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x", encoding="utf-8")
    assert ls.configure_file_logging(path=blocker / "sub" / "app.log") is None
    assert ls._existing_handler(logging.getLogger()) is None


def test_env_override_and_default_path(monkeypatch, tmp_path):
    monkeypatch.setenv("SPEC_CRITIC_LOG_PATH", str(tmp_path / "custom.log"))
    assert ls.default_log_path() == tmp_path / "custom.log"
    monkeypatch.delenv("SPEC_CRITIC_LOG_PATH")
    assert ls.default_log_path().name == "spec_critic.log"
    assert ".spec_critic" in str(ls.default_log_path())
