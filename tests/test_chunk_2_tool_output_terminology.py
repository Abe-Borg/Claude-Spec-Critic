"""Chunk 2 — best-effort tool-output terminology + diagnostics payload preservation.

Repair plan Chunk 2 directives:

1. The renamed helper :func:`structured_tool_output_enabled` is the canonical
   name; :func:`structured_outputs_enabled` is preserved as a deprecation
   alias so existing callers / tests keep working.
2. ``SPEC_CRITIC_STRUCTURED_TOOL_OUTPUT`` is the preferred env var, with the
   legacy ``SPEC_CRITIC_STRUCTURED_OUTPUTS`` accepted as a fallback.
3. The legacy ``_extract_json_array`` parser no longer stores the literal
   ``"[]"`` body as the "thinking" text for an empty-array response.
4. When the model invoked the custom tool, the parsed payload is preserved on
   the result objects and surfaced to diagnostics in a byte-bounded form.
"""
from __future__ import annotations

import importlib
import json

import pytest


# ---------------------------------------------------------------------------
# Helper-fixture: ensure both the schemas module and any downstream importers
# pick up env-var changes at test time. Several production modules cache the
# helper at import time, so we reload them after flipping env vars.
# ---------------------------------------------------------------------------


def _reload_schemas():
    from src import structured_schemas

    return importlib.reload(structured_schemas)


# ---------------------------------------------------------------------------
# 1. Terminology / env-var compatibility
# ---------------------------------------------------------------------------


class TestStructuredToolOutputFlag:
    def test_new_helper_default_on(self, monkeypatch):
        monkeypatch.delenv("SPEC_CRITIC_STRUCTURED_TOOL_OUTPUT", raising=False)
        monkeypatch.delenv("SPEC_CRITIC_STRUCTURED_OUTPUTS", raising=False)
        mod = _reload_schemas()
        assert mod.structured_tool_output_enabled() is True

    def test_legacy_helper_still_callable(self, monkeypatch):
        monkeypatch.delenv("SPEC_CRITIC_STRUCTURED_TOOL_OUTPUT", raising=False)
        monkeypatch.delenv("SPEC_CRITIC_STRUCTURED_OUTPUTS", raising=False)
        mod = _reload_schemas()
        # Deprecation alias must still be importable and return the same value.
        assert mod.structured_outputs_enabled() is True
        assert mod.structured_outputs_enabled() == mod.structured_tool_output_enabled()

    def test_new_env_var_disables(self, monkeypatch):
        monkeypatch.delenv("SPEC_CRITIC_STRUCTURED_OUTPUTS", raising=False)
        monkeypatch.setenv("SPEC_CRITIC_STRUCTURED_TOOL_OUTPUT", "0")
        mod = _reload_schemas()
        assert mod.structured_tool_output_enabled() is False
        assert mod.structured_outputs_enabled() is False

    def test_legacy_env_var_still_disables(self, monkeypatch):
        monkeypatch.delenv("SPEC_CRITIC_STRUCTURED_TOOL_OUTPUT", raising=False)
        monkeypatch.setenv("SPEC_CRITIC_STRUCTURED_OUTPUTS", "0")
        mod = _reload_schemas()
        # Legacy env var must keep working for at least one release.
        assert mod.structured_tool_output_enabled() is False
        assert mod.structured_outputs_enabled() is False

    def test_new_env_var_wins_when_both_set(self, monkeypatch):
        # Preferred name wins. Legacy says off, preferred says on → on.
        monkeypatch.setenv("SPEC_CRITIC_STRUCTURED_OUTPUTS", "0")
        monkeypatch.setenv("SPEC_CRITIC_STRUCTURED_TOOL_OUTPUT", "1")
        mod = _reload_schemas()
        assert mod.structured_tool_output_enabled() is True


class TestContractDocumentation:
    """The module docstring should describe what the code actually does."""

    def test_module_docstring_mentions_best_effort(self):
        mod = _reload_schemas()
        doc = (mod.__doc__ or "").lower()
        assert "best-effort" in doc
        assert "tool_choice" in doc and "auto" in doc

    def test_module_docstring_does_not_overclaim(self):
        """The renamed module should not claim a contractually guaranteed schema."""
        mod = _reload_schemas()
        doc = mod.__doc__ or ""
        # The module may explain *what Structured Outputs would be*, but the
        # phrase "guaranteed JSON-schema final response" appears only as a
        # negation; the new docstring must keep it that way.
        if "guaranteed JSON-schema" in doc:
            paragraph = next(
                p for p in doc.split("\n\n") if "guaranteed JSON-schema" in p
            )
            assert "not" in paragraph.lower(), (
                "module docstring claims a guarantee it does not enforce"
            )

    def test_review_tool_choice_comment_does_not_overclaim(self):
        # Inspect the helper source to ensure the comment matches reality.
        import inspect

        from src import structured_schemas

        src = inspect.getsource(structured_schemas.review_tool_choice)
        # The comment block should call out the best-effort nature.
        assert "not contractually" in src or "best-effort" in src.lower()


# ---------------------------------------------------------------------------
# 2. Empty-array parser bug fix
# ---------------------------------------------------------------------------


class TestExtractJsonArrayEmptyArrayBug:
    def test_empty_array_returns_empty_thinking(self):
        from src.reviewer import _extract_json_array

        data, thinking = _extract_json_array("[]")
        assert data == []
        # Chunk 2 fix: ``"[]"`` is the JSON body, not the model's thinking.
        # Returning it as the thinking text polluted the report's
        # analysis-summary field.
        assert thinking == ""

    def test_empty_array_with_surrounding_whitespace(self):
        from src.reviewer import _extract_json_array

        data, thinking = _extract_json_array("   []   ")
        assert data == []
        assert thinking == ""

    def test_findings_array_still_round_trips(self):
        from src.reviewer import _extract_json_array

        body = json.dumps([
            {"severity": "HIGH", "issue": "something", "actionType": "EDIT"}
        ])
        data, thinking = _extract_json_array(body)
        assert len(data) == 1
        # No findings_json tag and no preceding prose → thinking is empty.
        assert thinking == ""

    def test_thinking_prefix_preserved(self):
        from src.reviewer import _extract_json_array

        body = "I noticed one issue.\n\n" + json.dumps([
            {"severity": "HIGH", "issue": "something", "actionType": "EDIT"}
        ])
        data, thinking = _extract_json_array(body)
        assert len(data) == 1
        assert thinking.startswith("I noticed one issue.")


# ---------------------------------------------------------------------------
# 3. Structured payload preservation on ReviewResult / VerificationResult
# ---------------------------------------------------------------------------


class TestStructuredPayloadOnResult:
    def test_review_result_has_structured_payload_field(self):
        from src.reviewer import ReviewResult

        r = ReviewResult()
        # Default state: no tool payload captured yet.
        assert r.structured_payload is None

    def test_verification_result_has_structured_payload_field(self):
        from src.verifier import VerificationResult

        r = VerificationResult(verdict="UNVERIFIED")
        assert r.structured_payload is None

    def test_verdict_from_tool_use_preserves_payload(self, fake_anthropic):
        from src.verifier import _verdict_from_tool_use

        resp = fake_anthropic.verification_tool_use_response()
        result = _verdict_from_tool_use(resp)
        assert result is not None
        # The parsed tool input must be retained for diagnostics.
        assert isinstance(result.structured_payload, dict)
        assert result.structured_payload["verdict"] == "CONFIRMED"
        assert "explanation" in result.structured_payload

    def test_text_fallback_leaves_payload_none(self, fake_anthropic):
        from src.verifier import _parse_verification_response

        resp = fake_anthropic.verification_text_fallback_response()
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        result = _parse_verification_response(text)
        # No tool block → no structured payload.
        assert result.structured_payload is None

    def test_extract_structured_findings_returns_payload(self, fake_anthropic):
        from src.reviewer import _extract_structured_findings

        resp = fake_anthropic.review_tool_use_response()
        result = _extract_structured_findings(resp)
        assert result is not None
        data, summary, payload = result
        assert isinstance(payload, dict)
        # Payload should be the full structured tool input.
        assert "findings" in payload
        assert "analysis_summary" in payload

    def test_extract_structured_findings_returns_none_without_block(self, fake_anthropic):
        from src.reviewer import _extract_structured_findings

        resp = fake_anthropic.verification_text_fallback_response()
        # No matching review tool block → caller falls back to text parse.
        assert _extract_structured_findings(resp) is None


# ---------------------------------------------------------------------------
# 4. Diagnostics: byte-bounded structured payload capture
# ---------------------------------------------------------------------------


class TestDiagnosticsBoundedPayload:
    def test_bound_structured_payload_handles_none(self):
        from src.diagnostics import bound_structured_payload

        assert bound_structured_payload(None) is None

    def test_bound_structured_payload_serializes_small_dict(self):
        from src.diagnostics import bound_structured_payload

        bounded = bound_structured_payload({"verdict": "CONFIRMED", "sources": []})
        assert bounded is not None
        assert bounded["truncated"] is False
        assert json.loads(bounded["serialized"]) == {
            "verdict": "CONFIRMED",
            "sources": [],
        }
        assert bounded["bytes"] > 0

    def test_bound_structured_payload_truncates_large_input(self):
        from src.diagnostics import bound_structured_payload

        # A payload that easily exceeds the default 4096-byte cap.
        big = {"findings": ["x" * 100] * 200}
        bounded = bound_structured_payload(big)
        assert bounded is not None
        assert bounded["truncated"] is True
        # The serialized form must still be small enough to keep diagnostics
        # memory bounded.
        assert bounded["bytes"] <= 4096 + len("...(truncated)")
        assert bounded["serialized"].endswith("...(truncated)")

    def test_bound_structured_payload_respects_custom_cap(self):
        from src.diagnostics import bound_structured_payload

        payload = {"a": "hello world" * 20}
        bounded = bound_structured_payload(payload, max_bytes=32)
        assert bounded is not None
        assert bounded["truncated"] is True
        assert bounded["bytes"] <= 32 + len("...(truncated)")

    def test_bound_structured_payload_drops_unserializable(self):
        from src.diagnostics import bound_structured_payload

        class Weird:
            def __repr__(self):
                # Even repr is deterministic so default=str succeeds; this
                # asserts the helper does not blow up on odd objects.
                return "<weird>"

        bounded = bound_structured_payload({"weird": Weird()})
        # default=str salvages it; the helper records a serialized form.
        assert bounded is not None
        assert "<weird>" in bounded["serialized"]


class TestRecordApiCallStructuredPayload:
    def test_record_api_call_includes_payload(self):
        from src.diagnostics import DiagnosticsReport

        report = DiagnosticsReport()
        report.record_api_call(
            phase="review",
            model="claude-opus-4-7",
            structured_payload={"findings": [{"severity": "HIGH"}]},
        )
        evt = report.events[-1]
        assert evt.data is not None
        assert "structured_payload" in evt.data
        bounded = evt.data["structured_payload"]
        assert bounded["truncated"] is False
        assert "findings" in json.loads(bounded["serialized"])

    def test_record_api_call_omits_payload_when_none(self):
        from src.diagnostics import DiagnosticsReport

        report = DiagnosticsReport()
        report.record_api_call(phase="review", model="claude-opus-4-7")
        evt = report.events[-1]
        assert evt.data is not None
        # When no payload is supplied, the key is intentionally absent so
        # downstream summaries don't have to special-case ``None``.
        assert "structured_payload" not in evt.data

    def test_record_api_call_bounds_oversized_payload(self):
        from src.diagnostics import DiagnosticsReport

        report = DiagnosticsReport()
        big_payload = {"findings": [{"explanation": "x" * 200}] * 50}
        report.record_api_call(
            phase="review",
            structured_payload=big_payload,
        )
        evt = report.events[-1]
        bounded = evt.data["structured_payload"]
        assert bounded["truncated"] is True
        # Still well under typical event-data limits.
        assert bounded["bytes"] <= 4096 + len("...(truncated)")

    def test_record_api_call_extra_does_not_overwrite_payload(self):
        from src.diagnostics import DiagnosticsReport

        report = DiagnosticsReport()
        report.record_api_call(
            phase="review",
            structured_payload={"findings": []},
            extra={"structured_payload": "should-not-overwrite"},
        )
        evt = report.events[-1]
        # The bounded payload set by the helper wins; ``extra`` cannot stomp it.
        bounded = evt.data["structured_payload"]
        assert isinstance(bounded, dict)
        assert "serialized" in bounded
