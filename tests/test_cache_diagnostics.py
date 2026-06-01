"""Cache-diagnostics primitives (beta ``cache-diagnosis-2026-04-07``).

Opt-in observability for the cache-breakpoint-stability invariant: a request
carries ``diagnostics.previous_message_id`` and the response returns a
``diagnostics`` object describing the first cache-prefix divergence. These
tests pin the default-OFF gate, the (extra_body, extra_headers) builder, and
the defensive response extractor.
"""
from __future__ import annotations

import pytest

from src.core import api_config
from src.core.api_config import (
    CACHE_DIAGNOSTICS_BETA,
    ENV_CACHE_DIAGNOSTICS,
    cache_diagnostics_enabled,
    cache_diagnostics_params,
    extract_cache_diagnostics,
)


class TestEnabledGate:
    def test_disabled_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_CACHE_DIAGNOSTICS, raising=False)
        assert cache_diagnostics_enabled() is False

    @pytest.mark.parametrize("token", ["0", "false", "no", "off", ""])
    def test_disable_tokens(self, monkeypatch, token) -> None:
        monkeypatch.setenv(ENV_CACHE_DIAGNOSTICS, token)
        assert cache_diagnostics_enabled() is False

    @pytest.mark.parametrize("token", ["1", "true", "on", "yes"])
    def test_enable_tokens(self, monkeypatch, token) -> None:
        monkeypatch.setenv(ENV_CACHE_DIAGNOSTICS, token)
        assert cache_diagnostics_enabled() is True


class TestParamsBuilder:
    def test_noop_when_disabled(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_CACHE_DIAGNOSTICS, raising=False)
        assert cache_diagnostics_params("msg_123") == (None, None)

    def test_noop_without_previous_id_even_if_enabled(self, monkeypatch) -> None:
        # Diagnostics is meaningless without a prior message to diff against.
        monkeypatch.setenv(ENV_CACHE_DIAGNOSTICS, "1")
        assert cache_diagnostics_params(None) == (None, None)
        assert cache_diagnostics_params("") == (None, None)

    def test_builds_body_and_beta_header_when_enabled(self, monkeypatch) -> None:
        monkeypatch.setenv(ENV_CACHE_DIAGNOSTICS, "1")
        extra_body, extra_headers = cache_diagnostics_params("msg_abc")
        assert extra_body == {"diagnostics": {"previous_message_id": "msg_abc"}}
        assert extra_headers == {"anthropic-beta": CACHE_DIAGNOSTICS_BETA}

    def test_beta_value_is_pinned(self) -> None:
        assert CACHE_DIAGNOSTICS_BETA == "cache-diagnosis-2026-04-07"


class _FakeMessage:
    """Stand-in for an SDK Message with extra=allow behavior."""

    def __init__(self, diagnostics=None) -> None:
        if diagnostics is not None:
            self.diagnostics = diagnostics


class _DiagSubmodel:
    def model_dump(self):  # mimics a pydantic submodel
        return {"divergence_point": "system", "reason": "tools changed"}


class TestExtractor:
    def test_returns_none_when_absent(self) -> None:
        assert extract_cache_diagnostics(_FakeMessage()) is None

    def test_returns_dict_directly(self) -> None:
        diag = {"divergence_point": "messages[3]"}
        assert extract_cache_diagnostics(_FakeMessage(diag)) == diag

    def test_serializes_pydantic_submodel(self) -> None:
        out = extract_cache_diagnostics(_FakeMessage(_DiagSubmodel()))
        assert out == {"divergence_point": "system", "reason": "tools changed"}

    def test_never_raises_on_garbage(self) -> None:
        # A bare object with no diagnostics attr and no model_dump → None,
        # never an exception (a diagnostics read must not sink verification).
        assert extract_cache_diagnostics(object()) is None
        assert extract_cache_diagnostics(None) is None


class TestTraceHookNoOps:
    """The capture hook stamps nothing for a falsy diagnostics value, so a
    verification that did not request diagnostics adds no trace event."""

    def test_capture_hook_noop_on_none(self) -> None:
        from src.tracing import capture_hooks

        # Must not raise even with no active recorder / no handle.
        capture_hooks.capture_cache_diagnostics(None, diagnostics=None)
        capture_hooks.capture_cache_diagnostics(None, diagnostics={})
