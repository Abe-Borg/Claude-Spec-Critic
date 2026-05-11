"""Chunk A — verify the fake Anthropic fixtures match production parsers.

A fixture is only useful if production parsers consume it the same way
they consume a real SDK response. These tests round-trip each fake-response
case through the same helpers the streaming / batch paths use.

The five cases line up 1:1 with Chunk A directive 4:
  1. Structured review tool call.
  2. Structured verification verdict tool call.
  3. Verification response that stops with tool use.
  4. Verification response that falls back to plain JSON text.
  5. ``max_tokens`` incomplete response.
"""
from __future__ import annotations

from typing import Any

import pytest


pytestmark = pytest.mark.fixtures


def _required_keys_present(payload: Any, schema: dict[str, Any]) -> None:
    """Lightweight schema check — Chunk A keeps the test dep footprint small.

    Validates only the two properties we care about for fixture sanity:
    ``required`` keys exist on the object, and primitive-typed properties
    match a string/number/bool/array/object/null type when ``type`` is set.
    Anything more exotic should round-trip via the production parser
    rather than re-implement jsonschema here.
    """
    assert isinstance(payload, dict), f"expected object, got {type(payload).__name__}"
    for key in schema.get("required", []):
        assert key in payload, f"missing required key: {key}"
    for prop, sub in schema.get("properties", {}).items():
        if prop not in payload:
            continue
        types = sub.get("type")
        if not types:
            continue
        if isinstance(types, str):
            types = [types]
        value = payload[prop]
        ok = False
        for t in types:
            if t == "string" and isinstance(value, str):
                ok = True
            elif t == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
                ok = True
            elif t == "integer" and isinstance(value, int) and not isinstance(value, bool):
                ok = True
            elif t == "boolean" and isinstance(value, bool):
                ok = True
            elif t == "array" and isinstance(value, list):
                ok = True
            elif t == "object" and isinstance(value, dict):
                ok = True
            elif t == "null" and value is None:
                ok = True
        assert ok, f"{prop}={value!r} did not match any of {types}"


# ---------------------------------------------------------------------------
# Case 1: structured review tool call
# ---------------------------------------------------------------------------


class TestReviewToolUseResponse:
    def test_extracts_findings_payload_via_production_helper(self, fake_anthropic):
        from src.structured_schemas import REVIEW_TOOL_NAME, extract_tool_use_block

        resp = fake_anthropic.review_tool_use_response()
        payload = extract_tool_use_block(resp, REVIEW_TOOL_NAME)
        assert isinstance(payload, dict)
        assert "findings" in payload
        assert payload["findings"][0]["severity"] == "HIGH"

    def test_payload_matches_schema(self, fake_anthropic):
        from src.structured_schemas import REVIEW_FINDINGS_SCHEMA

        payload = fake_anthropic.sample_review_findings_payload()
        _required_keys_present(payload, REVIEW_FINDINGS_SCHEMA)
        finding_schema = REVIEW_FINDINGS_SCHEMA["properties"]["findings"]["items"]
        for finding in payload["findings"]:
            _required_keys_present(finding, finding_schema)

    def test_dict_shape_also_extractable(self, fake_anthropic):
        from src.structured_schemas import REVIEW_TOOL_NAME, extract_tool_use_block

        resp = fake_anthropic.review_tool_use_response(dict_shape=True)
        payload = extract_tool_use_block(resp, REVIEW_TOOL_NAME)
        assert isinstance(payload, dict)
        assert payload["findings"][0]["section"] == "2.1"

    def test_parse_findings_round_trip(self, fake_anthropic):
        from src.reviewer import _parse_findings
        from src.structured_schemas import REVIEW_TOOL_NAME, extract_tool_use_block

        resp = fake_anthropic.review_tool_use_response()
        payload = extract_tool_use_block(resp, REVIEW_TOOL_NAME)
        findings = _parse_findings(payload["findings"])
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "HIGH"
        assert f.actionType == "EDIT"
        assert f.fileName == "23 21 13 - Hydronic.docx"
        assert 0.0 <= f.confidence <= 1.0


# ---------------------------------------------------------------------------
# Case 2/3: structured verification verdict tool call (incl. tool_use stop)
# ---------------------------------------------------------------------------


class TestVerificationToolUseResponse:
    def test_extracts_verdict_payload(self, fake_anthropic):
        from src.structured_schemas import VERIFICATION_TOOL_NAME, extract_tool_use_block

        resp = fake_anthropic.verification_tool_use_response()
        payload = extract_tool_use_block(resp, VERIFICATION_TOOL_NAME)
        assert isinstance(payload, dict)
        assert payload["verdict"] == "CONFIRMED"
        assert payload["sources"]

    def test_payload_matches_schema(self, fake_anthropic):
        from src.structured_schemas import VERIFICATION_VERDICT_SCHEMA

        payload = fake_anthropic.sample_verification_verdict_payload()
        _required_keys_present(payload, VERIFICATION_VERDICT_SCHEMA)

    def test_verdict_from_tool_use_round_trip(self, fake_anthropic):
        from src.verifier import _verdict_from_tool_use

        resp = fake_anthropic.verification_tool_use_response()
        result = _verdict_from_tool_use(resp)
        assert result is not None
        assert result.verdict == "CONFIRMED"
        assert "dgs.ca.gov" in result.sources[0]

    def test_stop_reason_tool_use_is_default(self, fake_anthropic):
        resp = fake_anthropic.verification_tool_use_response()
        assert resp.stop_reason == "tool_use"

    def test_stop_reason_end_turn_override(self, fake_anthropic):
        resp = fake_anthropic.verification_tool_use_response(stop_reason="end_turn")
        assert resp.stop_reason == "end_turn"

    def test_includes_web_search_blocks_by_default(self, fake_anthropic):
        """Verifier grounding logic looks for web_search_tool_result blocks."""
        resp = fake_anthropic.verification_tool_use_response()
        kinds = {getattr(b, "type", None) for b in resp.content}
        assert "web_search_tool_result" in kinds
        assert "tool_use" in kinds

    def test_unknown_verdict_normalizes_to_unverified(self, fake_anthropic):
        from src.verifier import _verdict_from_tool_use

        resp = fake_anthropic.verification_tool_use_response(
            payload={
                "verdict": "MAYBE",
                "explanation": "unclear",
                "sources": [],
                "correction": None,
            },
        )
        result = _verdict_from_tool_use(resp)
        assert result is not None
        assert result.verdict == "UNVERIFIED"


# ---------------------------------------------------------------------------
# Case 4: verification response that falls back to plain JSON text
# ---------------------------------------------------------------------------


class TestVerificationTextFallbackResponse:
    def test_text_path_parses_verdict(self, fake_anthropic):
        from src.verifier import _parse_verification_response, _verdict_from_tool_use

        resp = fake_anthropic.verification_text_fallback_response()
        # No tool_use block — the tool extractor must return None.
        assert _verdict_from_tool_use(resp) is None
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        parsed = _parse_verification_response(text)
        assert parsed.verdict == "CONFIRMED"
        assert parsed.sources

    def test_dict_shape_text_path(self, fake_anthropic):
        from src.verifier import _verdict_from_tool_use

        resp = fake_anthropic.verification_text_fallback_response(dict_shape=True)
        # Dict-shape responses also have no tool_use block.
        assert _verdict_from_tool_use(resp) is None


# ---------------------------------------------------------------------------
# Case 5: ``max_tokens`` incomplete response
# ---------------------------------------------------------------------------


class TestMaxTokensIncompleteResponse:
    def test_stop_reason_is_max_tokens(self, fake_anthropic):
        resp = fake_anthropic.max_tokens_incomplete_response()
        assert resp.stop_reason == "max_tokens"

    def test_tool_extractor_returns_none(self, fake_anthropic):
        from src.structured_schemas import REVIEW_TOOL_NAME, extract_tool_use_block

        resp = fake_anthropic.max_tokens_incomplete_response()
        assert extract_tool_use_block(resp, REVIEW_TOOL_NAME) is None

    def test_partial_text_is_preserved(self, fake_anthropic):
        resp = fake_anthropic.max_tokens_incomplete_response(partial_text="incomplete")
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        assert "incomplete" in text


# ---------------------------------------------------------------------------
# Batch envelopes
# ---------------------------------------------------------------------------


class TestBatchResultEnvelopes:
    def test_review_envelope_succeeded(self, fake_anthropic):
        env = fake_anthropic.batch_review_result(custom_id="review__A__0")
        assert env.custom_id == "review__A__0"
        assert env.result.type == "succeeded"
        assert env.result.message is not None

    def test_verification_envelope_succeeded(self, fake_anthropic):
        env = fake_anthropic.batch_verification_result(custom_id="verify__0")
        assert env.custom_id == "verify__0"
        assert env.result.type == "succeeded"

    def test_errored_envelope(self, fake_anthropic):
        env = fake_anthropic.batch_errored_result(
            custom_id="review__A__0", error_message="invalid_request"
        )
        assert env.result.type == "errored"
        assert "invalid_request" in env.result.error.message
