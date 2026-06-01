"""Strict tool use is an env-gated, default-OFF lever.

``strict: true`` grammar-constrains tool input to the declared schema, which
would eliminate the malformed-payload failure mode the tagged-JSON fallback
parsers absorb. It defaults OFF because the strict+adaptive-thinking
interaction is unverified from the hermetic harness and a wrong default would
400 every submit (see ``structured_schemas._strict_enabled``). These tests pin
the gate: byte-identical tool defs by default, ``strict: true`` only on opt-in.
"""
from __future__ import annotations

import pytest

from src.review import structured_schemas as ss
from src.review.structured_schemas import (
    ENV_STRICT_TOOL_USE,
    cross_check_findings_tool,
    review_findings_tool,
    triage_classifications_tool,
    verification_verdict_tool,
)

_ALL_TOOL_BUILDERS = (
    review_findings_tool,
    cross_check_findings_tool,
    verification_verdict_tool,
    triage_classifications_tool,
)


class TestStrictGateDefault:
    def test_disabled_when_env_unset(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_STRICT_TOOL_USE, raising=False)
        assert ss._strict_enabled() is False

    @pytest.mark.parametrize("token", ["0", "false", "no", "off", "OFF", "  False  ", ""])
    def test_disable_tokens(self, monkeypatch, token) -> None:
        monkeypatch.setenv(ENV_STRICT_TOOL_USE, token)
        assert ss._strict_enabled() is False

    @pytest.mark.parametrize("token", ["1", "true", "yes", "on", "anything"])
    def test_enable_tokens(self, monkeypatch, token) -> None:
        monkeypatch.setenv(ENV_STRICT_TOOL_USE, token)
        assert ss._strict_enabled() is True


class TestToolsCarryStrictOnlyWhenEnabled:
    def test_no_strict_key_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_STRICT_TOOL_USE, raising=False)
        for builder in _ALL_TOOL_BUILDERS:
            tool = builder()
            assert "strict" not in tool, f"{builder.__name__} leaked strict by default"

    def test_strict_true_when_enabled(self, monkeypatch) -> None:
        monkeypatch.setenv(ENV_STRICT_TOOL_USE, "1")
        for builder in _ALL_TOOL_BUILDERS:
            tool = builder()
            assert tool.get("strict") is True, f"{builder.__name__} missing strict"
            # The schema itself is unchanged — strict is purely additive.
            assert "input_schema" in tool
