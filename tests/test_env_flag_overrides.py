"""Regression tests for operator-switch env vars.

These flags were previously documented in docstrings/comments but the
underlying helpers were hardcoded to ``return True`` / ``return 0`` /
``return Path.home() / ...``. The tests below pin the actual env-var
parsing so the docs and the behavior stay in sync.

All tests use ``monkeypatch`` so a stray env var in the developer's shell
does not bleed into the assertions, and so flipping a flag in one test
never persists into another.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.verification import verification_cache
from src.review import prompt_serialization


# ---------------------------------------------------------------------------
# SPEC_CRITIC_ELEMENT_IDS
# ---------------------------------------------------------------------------


def test_element_ids_enabled_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPEC_CRITIC_ELEMENT_IDS", raising=False)
    assert prompt_serialization.element_ids_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "No", "off", " 0 "])
def test_element_ids_disabled_via_env(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("SPEC_CRITIC_ELEMENT_IDS", value)
    assert prompt_serialization.element_ids_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "anything-else", ""])
def test_element_ids_other_values_keep_default(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("SPEC_CRITIC_ELEMENT_IDS", value)
    assert prompt_serialization.element_ids_enabled() is True


# ---------------------------------------------------------------------------
# SPEC_CRITIC_VERIFICATION_CACHE_PERSIST
# ---------------------------------------------------------------------------


def test_cache_persist_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_CACHE_PERSIST", raising=False)
    assert verification_cache.cache_persist_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "No", "OFF"])
def test_cache_persist_disabled_via_env(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_PERSIST", value)
    assert verification_cache.cache_persist_enabled() is False


# ---------------------------------------------------------------------------
# SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS
# ---------------------------------------------------------------------------


def test_cache_ttl_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # The unset default is 60 days, balancing
    # reuse against staleness for code references that may have new
    # amendments published quarterly. Operators can explicitly opt out
    # via SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS=0 to keep the legacy
    # "no expiry" behavior.
    monkeypatch.delenv("SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS", raising=False)
    assert verification_cache.cache_ttl_days() == 60


def test_cache_ttl_positive_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS", "30")
    assert verification_cache.cache_ttl_days() == 30


def test_cache_ttl_explicit_zero_means_no_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``0`` is an operator-explicit "no expiry"
    # override and must be preserved verbatim — the legacy database-mode
    # behavior remains available for users who want it.
    monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS", "0")
    assert verification_cache.cache_ttl_days() == 0


@pytest.mark.parametrize("value", ["", "  ", "not-a-number", "-7"])
def test_cache_ttl_invalid_or_negative_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Malformed or negative values fall back to the 60-day default.

    The previous behavior (fall back to 0 = no
    expiry) silently disabled expiry on any typo. The new behavior falls
    back to the 60-day default so a typo never accidentally turns the
    cache into a permanent database. Explicit ``0`` is still honored
    (separate test above) for operators who want the legacy behavior.
    """
    monkeypatch.setenv("SPEC_CRITIC_VERIFICATION_CACHE_TTL_DAYS", value)
    assert verification_cache.cache_ttl_days() == 60


# ---------------------------------------------------------------------------
# SPEC_CRITIC_CACHE_PATH
# ---------------------------------------------------------------------------


def test_cache_path_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPEC_CRITIC_CACHE_PATH", raising=False)
    expected = Path.home() / ".spec_critic" / "verification_cache.json"
    assert verification_cache.default_cache_path() == expected


def test_cache_path_absolute_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "alt_cache.json"
    monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", str(target))
    assert verification_cache.default_cache_path() == target


def test_cache_path_expands_user_and_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SPEC_CRITIC_FAKE_HOME", str(tmp_path))
    monkeypatch.setenv(
        "SPEC_CRITIC_CACHE_PATH", "$SPEC_CRITIC_FAKE_HOME/cache.json"
    )
    assert verification_cache.default_cache_path() == tmp_path / "cache.json"


def test_cache_path_blank_override_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", "   ")
    expected = Path.home() / ".spec_critic" / "verification_cache.json"
    assert verification_cache.default_cache_path() == expected


def test_cache_save_and_load_respect_path_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The override flows through ``save_to_disk`` / ``load_from_disk``."""
    target = tmp_path / "nested" / "cache.json"
    monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", str(target))

    cache = verification_cache.VerificationCache()
    # No entries — save still writes the header so load can round-trip.
    count = cache.save_to_disk()
    assert count == 0
    assert target.exists()

    fresh = verification_cache.VerificationCache()
    loaded = fresh.load_from_disk()
    assert loaded == 0
