"""Text-fallback JSON parsing and tool-name matching.

Two recommendations from Anthropic's Sonnet 5.5 prompting guide (checked
2026-09-29), applied to every parser that reads a model's answer:

* "Reasoning tasks with JSON output": read the *last* JSON value in the
  response, found by trying each ``{`` / ``[`` in turn and skipping a parsed
  value whole — never the span from the first ``{`` to the last ``}``, because
  the model occasionally writes a draft before its final JSON.
* "Tolerant tool-call handling": the model occasionally calls a declared tool
  by a name that differs only in letter case; accept the call when the match
  is unambiguous.

Reproduced on master ``5a66bb7`` before the change:

* a review answered in untagged text whose last finding quoted a bracketed
  placeholder (``[SELECT]``, common in specifications) raised "Could not
  extract JSON findings" — a parse error, and a paid repair;
* a verifier text verdict preceded by a draft, or by prose with a brace in it,
  was "not valid JSON" — a verification failure;
* research and compliance tagged fallbacks matched greedily from the first
  opening tag to the last closing tag, so a draft block and a final block read
  as one invalid body; drawing impact read the first-to-last brace span;
* ``submit_Review_Findings`` was not the review tool.

The verifier cases run through both transports in
``test_verdict_classification_contract.py``.
"""
from __future__ import annotations

import json

import pytest

from src.compliance.compliance_checker import _parse_compliance_payload
from src.drawing_impact.impact_synthesizer import _extract_impact_object
from src.research.requirements_research import _parse_research_payload
from src.review.reviewer import (
    PARSE_SOURCE_TEXT,
    PARSE_SOURCE_TOOL,
    _extract_json_array,
    review_result_from_message,
)
from src.review.structured_schemas import (
    COMPLIANCE_TOOL_NAME,
    REVIEW_TOOL_NAME,
    extract_tool_use_block,
    json_values_in_text,
    last_tagged_json_object,
    tool_name_matches,
)
from tests.fixtures.fake_anthropic import (
    FakeMessage,
    FakeTextBlock,
    FakeToolUseBlock,
    sample_review_findings_payload,
)


def _finding(issue: str, severity: str = "HIGH") -> dict:
    return {"severity": severity, "issue": issue, "actionType": "REPORT_ONLY"}


# ---------------------------------------------------------------------------
# The shared scanner
# ---------------------------------------------------------------------------


class TestJsonValuesInText:
    def test_values_in_order_with_their_spans(self):
        text = 'a {"x": 1} b [2, 3] c'
        values = json_values_in_text(text)
        assert [v for _s, _e, v in values] == [{"x": 1}, [2, 3]]
        for start, end, value in values:
            assert json.loads(text[start:end]) == value

    def test_nested_values_are_not_counted_on_their_own(self):
        values = json_values_in_text('[{"a": [1, {"b": 2}]}]')
        assert len(values) == 1

    def test_a_bracket_in_prose_is_skipped(self):
        values = json_values_in_text('Placeholder [SELECT] remains. {"ok": true}')
        assert [v for _s, _e, v in values] == [{"ok": True}]

    def test_a_draft_and_a_final_value_are_both_found(self):
        values = json_values_in_text('draft {"v": 1} final {"v": 2}')
        assert [v for _s, _e, v in values] == [{"v": 1}, {"v": 2}]

    def test_no_json(self):
        assert json_values_in_text("") == []
        assert json_values_in_text("no json { here [ at all") == []

    def test_deep_nesting_does_not_raise(self):
        # Deeper than the decoder's recursion limit: skipped, never raised.
        assert json_values_in_text("[" * 5_000) == []

    def test_long_prose_full_of_brackets_stays_fast(self):
        # A failed parse builds an error that counts the lines before it, so
        # trying every prose bracket was quadratic; only brackets that can
        # open a value are tried now.
        import time

        text = "Replace [SELECT] with {value} per 2.1.A. " * 20_000
        text += json.dumps([_finding("Placeholder [SELECT] in 2.1.A")])
        started = time.perf_counter()
        values = json_values_in_text(text)
        assert time.perf_counter() - started < 1.0
        assert values[-1][2] == [_finding("Placeholder [SELECT] in 2.1.A")]

    def test_whitespace_and_every_value_start(self):
        text = '[ 1, 2 ] { "a" : [ ] } [\n{"b": null}] [-1] [true] [] {}'
        assert [v for _s, _e, v in json_values_in_text(text)] == [
            [1, 2], {"a": []}, [{"b": None}], [-1], [True], [], {},
        ]


class TestLastTaggedJsonObject:
    def test_the_last_block_that_parses_wins(self):
        text = '<t>{"v": 1}</t> then <t>{"v": 2}</t>'
        assert last_tagged_json_object(text, "t") == {"v": 2}

    def test_an_unparseable_final_block_falls_back_to_the_one_before(self):
        text = '<t>{"v": 1}</t> then <t>{"v": 2</t>'
        assert last_tagged_json_object(text, "t") == {"v": 1}

    def test_a_non_object_body_is_not_an_answer(self):
        assert last_tagged_json_object("<t>[1, 2]</t>", "t") is None
        assert last_tagged_json_object("nothing", "t") is None


# ---------------------------------------------------------------------------
# Review (and cross-check, which shares the parser)
# ---------------------------------------------------------------------------


class TestReviewTextFallback:
    def test_a_bracketed_placeholder_in_the_last_finding(self):
        text = "Analysis.\n" + json.dumps([_finding("Placeholder [SELECT] left in 2.1.A")])
        data, thinking = _extract_json_array(text)
        assert data == [_finding("Placeholder [SELECT] left in 2.1.A")]
        assert thinking == "Analysis."

    def test_a_draft_block_then_the_final_block(self):
        draft = json.dumps([_finding("draft")])
        final = json.dumps([_finding("final one"), _finding("final two")])
        text = f"<findings_json>{draft}</findings_json>\nRevised:\n<findings_json>{final}</findings_json>"
        data, _thinking = _extract_json_array(text)
        assert [f["issue"] for f in data] == ["final one", "final two"]

    def test_an_object_carrying_the_findings(self):
        text = "Summary first.\n" + json.dumps(
            {"analysis_summary": "s", "findings": [_finding("Quotes [VERIFY] marker")]}
        )
        data, thinking = _extract_json_array(text)
        assert [f["issue"] for f in data] == ["Quotes [VERIFY] marker"]
        assert thinking == "Summary first."

    def test_unrelated_json_after_the_findings_is_passed_over(self):
        text = json.dumps([_finding("real")]) + '\nSee also [1] and {"note": "x"}.'
        data, _thinking = _extract_json_array(text)
        assert [f["issue"] for f in data] == ["real"]

    def test_an_empty_array_is_no_findings(self):
        assert _extract_json_array("[]") == ([], "")
        assert _extract_json_array("Nothing to report.\n[]") == ([], "Nothing to report.")

    def test_no_findings_json_still_raises(self):
        with pytest.raises(ValueError):
            _extract_json_array('Only prose, and {"not": "findings"}.')

    def test_end_to_end_the_review_is_read_not_failed(self):
        text = "Review complete.\n" + json.dumps([_finding("Placeholder [SELECT] in 1.02")])
        message = FakeMessage(content=[FakeTextBlock(text=text)], stop_reason="end_turn")
        result = review_result_from_message(message, model="claude-opus-5-5")
        assert result.parse_status == "ok"
        assert result.parse_source == PARSE_SOURCE_TEXT
        assert [f.issue for f in result.findings] == ["Placeholder [SELECT] in 1.02"]


# ---------------------------------------------------------------------------
# Research, compliance, drawing impact
# ---------------------------------------------------------------------------


class TestTaggedFallbacks:
    def test_research_reads_the_last_block(self):
        text = (
            '<research_json>{"items": [], "notes": "draft"}</research_json>\n'
            '<research_json>{"items": [], "notes": "final"}</research_json>'
        )
        response = FakeMessage(content=[FakeTextBlock(text=text)])
        payload, source = _parse_research_payload([response])
        assert source == "text_fallback"
        assert payload["notes"] == "final"

    def test_compliance_reads_the_last_block(self, monkeypatch):
        text = (
            '<compliance_json>{"findings": [], "coverage": [], "v": 1}</compliance_json> '
            '<compliance_json>{"findings": [], "coverage": [], "v": 2}</compliance_json>'
        )
        response = FakeMessage(content=[FakeTextBlock(text=text)])
        payload, source = _parse_compliance_payload(response, text)
        assert source == "text_fallback"
        assert payload["v"] == 2

    def test_drawing_impact_draft_then_final_without_tags(self):
        raw = (
            'Draft: {"impact_level": "none", "narrative": "d"}\n'
            'Final: {"impact_level": "moderate", "narrative": "f"}'
        )
        assert _extract_impact_object(raw)["impact_level"] == "moderate"

    def test_drawing_impact_prose_braces_before_the_object(self):
        raw = 'Sheet notes use {braces}. {"impact_level": "minimal", "narrative": "n"}'
        assert _extract_impact_object(raw)["impact_level"] == "minimal"

    def test_drawing_impact_prefers_the_tagged_block(self):
        raw = (
            '{"impact_level": "none"} <drawing_impact_json>'
            '{"impact_level": "substantial"}</drawing_impact_json>'
        )
        assert _extract_impact_object(raw)["impact_level"] == "substantial"


# ---------------------------------------------------------------------------
# Tool names
# ---------------------------------------------------------------------------


class TestToolNameMatching:
    @pytest.mark.parametrize(
        "name, expected",
        [
            ("submit_review_findings", True),
            ("Submit_Review_Findings", True),
            ("SUBMIT_REVIEW_FINDINGS", True),
            ("submit_review_finding", False),
            ("submit-review-findings", False),
            ("", False),
            (None, False),
        ],
    )
    def test_case_only_differences_match(self, name, expected):
        assert tool_name_matches(name, REVIEW_TOOL_NAME) is expected

    def test_extract_accepts_a_case_variant(self):
        message = FakeMessage(
            content=[FakeToolUseBlock(name="Submit_Review_Findings", input={"findings": []})]
        )
        assert extract_tool_use_block(message, REVIEW_TOOL_NAME) == {"findings": []}

    def test_extract_ignores_another_tool(self):
        message = FakeMessage(
            content=[FakeToolUseBlock(name=COMPLIANCE_TOOL_NAME, input={"findings": []})]
        )
        assert extract_tool_use_block(message, REVIEW_TOOL_NAME) is None

    def test_review_submitted_under_a_case_variant_is_read_as_the_tool(self):
        message = FakeMessage(
            content=[
                FakeToolUseBlock(
                    name="Submit_Review_Findings", input=sample_review_findings_payload()
                )
            ],
            stop_reason="tool_use",
        )
        result = review_result_from_message(message, model="claude-sonnet-5-5")
        assert result.parse_status == "ok"
        assert result.parse_source == PARSE_SOURCE_TOOL
        assert result.findings


class TestCoordinationTextFallback:
    """The EX-06 coordination pass reads its text fallback the same way:
    the last tagged block that parses, else the last object carrying an
    ``observations`` list — never the first-``{``-to-last-``}`` span."""

    @staticmethod
    def _read(text):
        from src.coordination.adjudication import _extract_object

        return _extract_object(text)

    def test_a_draft_block_before_the_final_one(self):
        text = (
            '<coordination_json>{"observations": [{"candidate_id": "draft"}]}'
            "</coordination_json>\nOn reflection:\n"
            '<coordination_json>{"observations": [{"candidate_id": "final"}]}'
            "</coordination_json>"
        )
        assert self._read(text) == {"observations": [{"candidate_id": "final"}]}

    def test_a_last_block_that_does_not_parse_leaves_the_earlier_one(self):
        text = (
            '<coordination_json>{"observations": []}</coordination_json>'
            "<coordination_json>{not json</coordination_json>"
        )
        assert self._read(text) == {"observations": []}

    def test_a_brace_in_the_prose_does_not_hide_the_object(self):
        text = (
            "Pair {c-1} needs a look.\n"
            '{"observations": [{"candidate_id": "c-1"}]}'
        )
        assert self._read(text) == {"observations": [{"candidate_id": "c-1"}]}

    def test_the_last_untagged_object_wins(self):
        text = '{"observations": [1]} then {"observations": [2]}'
        assert self._read(text) == {"observations": [2]}

    def test_an_object_without_observations_is_not_the_payload(self):
        assert self._read('{"verdict": "CONFIRMED"}') is None
        assert self._read("") is None
