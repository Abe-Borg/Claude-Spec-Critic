"""Strict tool use is an env-gated, default-ON lever.

``strict: true`` grammar-constrains tool input to the declared schema,
eliminating the malformed-payload failure mode the tagged-JSON fallback
parsers absorb. It defaults ON: Anthropic's structured-outputs docs list
strict tool use as compatible with adaptive thinking, streaming, and the
Message Batches API, and ``tests/test_network_smoke.py::
test_strict_tool_use_smoke`` sends the exact production strict shape live.
``SPEC_CRITIC_STRICT_TOOL_USE=0`` is the rollback to the legacy lenient
shape (see ``structured_schemas._strict_enabled``). These tests pin the
gate: ``strict: true`` on every tool by default, byte-identical legacy tool
defs on opt-out.
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
    def test_enabled_when_env_unset(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_STRICT_TOOL_USE, raising=False)
        assert ss._strict_enabled() is True

    @pytest.mark.parametrize("token", ["0", "false", "no", "off", "OFF", "  False  "])
    def test_disable_tokens(self, monkeypatch, token) -> None:
        monkeypatch.setenv(ENV_STRICT_TOOL_USE, token)
        assert ss._strict_enabled() is False

    @pytest.mark.parametrize("token", ["1", "true", "yes", "on", "anything", ""])
    def test_non_disable_values_stay_enabled(self, monkeypatch, token) -> None:
        # Matches the house default-ON flag convention (SPEC_CRITIC_ELEMENT_IDS
        # et al.): only a recognized disable token turns the flag off; any
        # other value — including empty — leaves the default-enabled behavior.
        monkeypatch.setenv(ENV_STRICT_TOOL_USE, token)
        assert ss._strict_enabled() is True


class TestToolsCarryStrictUnlessDisabled:
    def test_strict_true_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_STRICT_TOOL_USE, raising=False)
        for builder in _ALL_TOOL_BUILDERS:
            tool = builder()
            assert tool.get("strict") is True, f"{builder.__name__} missing strict by default"
            # The schema itself is unchanged — strict is purely additive.
            assert "input_schema" in tool

    def test_no_strict_key_when_disabled(self, monkeypatch) -> None:
        monkeypatch.setenv(ENV_STRICT_TOOL_USE, "0")
        for builder in _ALL_TOOL_BUILDERS:
            tool = builder()
            # The rollback must restore the byte-identical legacy tool shape —
            # no leftover ``strict`` key, not even ``strict: false``.
            assert "strict" not in tool, f"{builder.__name__} leaked strict when disabled"


class TestSchemasStayInsideStrictSubset:
    """The strict-mode supported subset excludes numerical/string constraints.

    ``minimum`` / ``maximum`` / ``minLength`` / ``maxLength`` / ``oneOf`` /
    ``anyOf`` must never reappear in a tool schema: with ``strict: true`` now
    the default, an out-of-subset keyword risks a hard 400 at submit. Range
    enforcement lives at parse time instead (confidence clamp; triage
    index-membership filter).
    """

    _FORBIDDEN_KEYS = {"minimum", "maximum", "minLength", "maxLength", "oneOf", "anyOf"}

    def _walk(self, node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key not in self._FORBIDDEN_KEYS, (
                    f"strict-incompatible keyword {key!r} at {path or '<root>'}"
                )
                self._walk(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                self._walk(value, f"{path}[{i}]")

    @pytest.mark.parametrize("builder", _ALL_TOOL_BUILDERS)
    def test_no_out_of_subset_keywords(self, monkeypatch, builder) -> None:
        monkeypatch.delenv(ENV_STRICT_TOOL_USE, raising=False)
        self._walk(builder()["input_schema"])
