"""Hermetic tests for the LLM-as-judge eval matcher (:mod:`evals.judge`).

The judge only ever runs on the paid ``--live`` capture path, so these
tests pin its pure parts: tool schemas stay inside the strict subset,
responses are membership-validated like triage, every failure path
degrades to ``None`` (the substring-fallback signal) instead of raising,
and the judge-backed matcher drives ``score_spec_review`` through the
same protocol as the substring default.
"""
from __future__ import annotations

import pytest

from evals import judge
from evals.labeled_specs import (
    ExpectedDefect,
    LabeledSpec,
    defect_matched,
    score_spec_review,
)
from src.core.api_config import MODEL_HAIKU_45
from src.review.reviewer import Finding
from src.review.structured_schemas import ENV_STRICT_TOOL_USE
from tests.fixtures.fake_anthropic import FakeMessage, FakeToolUseBlock


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _spec() -> LabeledSpec:
    return LabeledSpec(
        spec_id="judge_test",
        filename="23 31 13 - Ductwork (judge test).docx",
        spec_text=(
            "PART 2 PRODUCTS\n"
            "A. Provide ductwork rated for 2 inches w.g.\n"
            "B. All supply ductwork shall be constructed for 4 inches w.g.\n"
        ),
        expected_defects=(
            ExpectedDefect(
                label="Duct pressure class stated as both 2 and 4 in. w.g.",
                expected_severity="HIGH",
                must_match=("w.g.",),
            ),
        ),
    )


def _paraphrased_finding() -> Finding:
    # Identifies the labeled defect but never writes the "w.g." token, so
    # the substring matcher misses it — the judge's reason for existing.
    return Finding(
        severity="HIGH",
        fileName="23 31 13 - Ductwork (judge test).docx",
        section="2.01",
        issue="Pressure class contradiction: paragraph A requires 2 inches of "
        "water gauge while paragraph B requires 4 inches of water gauge.",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference=None,
    )


def _unrelated_finding() -> Finding:
    return Finding(
        severity="GRIPES",
        fileName="23 31 13 - Ductwork (judge test).docx",
        section="2.01",
        issue="Section heading style is inconsistent with the project template.",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference=None,
    )


class _FakeClient:
    """Minimal ``client.messages.create`` double for the judge calls."""

    def __init__(self, response: FakeMessage | None = None, exc: Exception | None = None):
        self._response = response
        self._exc = exc
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._response


def _match_response(entries: list[dict]) -> FakeMessage:
    return FakeMessage(
        content=[FakeToolUseBlock(name="submit_defect_matches", input={"matches": entries})]
    )


def _classify_response(entries: list[dict]) -> FakeMessage:
    return FakeMessage(
        content=[
            FakeToolUseBlock(
                name="submit_extra_finding_classifications",
                input={"classifications": entries},
            )
        ]
    )


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------


class TestJudgeToolSchemas:
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

    @pytest.mark.parametrize(
        "builder", [judge.defect_matches_tool, judge.extra_classifications_tool]
    )
    def test_schemas_stay_inside_strict_subset(self, monkeypatch, builder) -> None:
        monkeypatch.delenv(ENV_STRICT_TOOL_USE, raising=False)
        self._walk(builder(model=MODEL_HAIKU_45)["input_schema"])

    @pytest.mark.parametrize(
        "builder", [judge.defect_matches_tool, judge.extra_classifications_tool]
    )
    def test_strict_gating_matches_production_tools(self, monkeypatch, builder) -> None:
        monkeypatch.delenv(ENV_STRICT_TOOL_USE, raising=False)
        assert builder(model=MODEL_HAIKU_45).get("strict") is True
        assert "strict" not in builder(model="claude-not-a-model")
        monkeypatch.setenv(ENV_STRICT_TOOL_USE, "0")
        assert "strict" not in builder(model=MODEL_HAIKU_45)


# ---------------------------------------------------------------------------
# Defect matching
# ---------------------------------------------------------------------------


class TestJudgeDefectMatches:
    def test_happy_path_match(self) -> None:
        client = _FakeClient(
            _match_response(
                [{"defect_index": 0, "finding_index": 1, "reasoning": "Same contradiction."}]
            )
        )
        spec = _spec()
        findings = [_unrelated_finding(), _paraphrased_finding()]
        matches = judge.judge_defect_matches(
            spec, findings, model=MODEL_HAIKU_45, client=client
        )
        assert matches is not None
        assert matches[0].finding_index == 1
        assert len(client.calls) == 1

    def test_explicit_null_is_a_miss_not_a_failure(self) -> None:
        client = _FakeClient(
            _match_response([{"defect_index": 0, "finding_index": None, "reasoning": "No match."}])
        )
        matches = judge.judge_defect_matches(
            _spec(), [_unrelated_finding()], model=MODEL_HAIKU_45, client=client
        )
        assert matches is not None
        assert matches[0].finding_index is None

    def test_no_findings_short_circuits_without_a_call(self) -> None:
        client = _FakeClient()
        matches = judge.judge_defect_matches(
            _spec(), [], model=MODEL_HAIKU_45, client=client
        )
        assert matches is not None
        assert matches[0].finding_index is None
        assert client.calls == []

    def test_no_defects_short_circuits_without_a_call(self) -> None:
        client = _FakeClient()
        clean = LabeledSpec(
            spec_id="clean", filename="c.docx", spec_text="A.", is_clean=True
        )
        assert judge.judge_defect_matches(
            clean, [_unrelated_finding()], model=MODEL_HAIKU_45, client=client
        ) == {}
        assert client.calls == []

    def test_incomplete_coverage_falls_back(self) -> None:
        # Coverage is all-or-nothing: an empty matches array on a spec with
        # one defect means the judge malfunctioned.
        client = _FakeClient(_match_response([]))
        assert (
            judge.judge_defect_matches(
                _spec(), [_paraphrased_finding()], model=MODEL_HAIKU_45, client=client
            )
            is None
        )

    def test_out_of_range_finding_index_falls_back(self) -> None:
        client = _FakeClient(
            _match_response([{"defect_index": 0, "finding_index": 7, "reasoning": "x"}])
        )
        assert (
            judge.judge_defect_matches(
                _spec(), [_paraphrased_finding()], model=MODEL_HAIKU_45, client=client
            )
            is None
        )

    def test_api_error_falls_back(self) -> None:
        client = _FakeClient(exc=RuntimeError("boom"))
        assert (
            judge.judge_defect_matches(
                _spec(), [_paraphrased_finding()], model=MODEL_HAIKU_45, client=client
            )
            is None
        )

    def test_text_only_response_falls_back(self) -> None:
        from tests.fixtures.fake_anthropic import FakeTextBlock

        client = _FakeClient(FakeMessage(content=[FakeTextBlock(text="no tool call")]))
        assert (
            judge.judge_defect_matches(
                _spec(), [_paraphrased_finding()], model=MODEL_HAIKU_45, client=client
            )
            is None
        )


# ---------------------------------------------------------------------------
# Extra-finding classification
# ---------------------------------------------------------------------------


class TestClassifyExtras:
    def test_happy_path_and_membership_filter(self) -> None:
        client = _FakeClient(
            _classify_response(
                [
                    {"finding_index": 0, "classification": "hallucination", "reasoning": "Not in spec."},
                    # Not in the extra set — must be dropped (triage-style filter).
                    {"finding_index": 5, "classification": "hallucination", "reasoning": "x"},
                    # Unknown label — dropped.
                    {"finding_index": 1, "classification": "sorta_fine", "reasoning": "x"},
                ]
            )
        )
        result = judge.classify_extra_findings(
            _spec(),
            [_unrelated_finding(), _unrelated_finding()],
            [0, 1],
            model=MODEL_HAIKU_45,
            client=client,
        )
        assert result is not None
        assert set(result) == {0}
        assert result[0].classification == "hallucination"

    def test_prompt_includes_matched_context(self) -> None:
        """duplicate_of_matched is only decidable when the judge sees what
        was matched — the prompt must carry the defect labels and the matched
        finding bodies (Codex P2 on #282)."""
        spec = _spec()
        findings = [_paraphrased_finding(), _unrelated_finding()]
        prompt = judge._build_classify_prompt(
            spec, findings, [1], matched_pairs=[(0, 0)]
        )
        assert "<matched_context>" in prompt
        assert spec.expected_defects[0].label in prompt
        # The matched finding's body is rendered for reference.
        assert "water gauge" in prompt
        # A missed defect renders as context too, marked unmatched.
        miss_prompt = judge._build_classify_prompt(
            spec, findings, [1], matched_pairs=[(0, None)]
        )
        assert "none (missed)" in miss_prompt

    def test_matched_context_forwarded_to_the_judge_call(self) -> None:
        client = _FakeClient(_classify_response([]))
        spec = _spec()
        findings = [_paraphrased_finding(), _unrelated_finding()]
        judge.classify_extra_findings(
            spec,
            findings,
            [1],
            matched_pairs=[(0, 0)],
            model=MODEL_HAIKU_45,
            client=client,
        )
        sent = client.calls[0]["messages"][0]["content"]
        assert "<matched_context>" in sent
        assert spec.expected_defects[0].label in sent

    def test_no_extras_short_circuits_without_a_call(self) -> None:
        client = _FakeClient()
        assert (
            judge.classify_extra_findings(
                _spec(), [_paraphrased_finding()], [], model=MODEL_HAIKU_45, client=client
            )
            == {}
        )
        assert client.calls == []

    def test_api_error_returns_none(self) -> None:
        client = _FakeClient(exc=RuntimeError("boom"))
        assert (
            judge.classify_extra_findings(
                _spec(), [_unrelated_finding()], [0], model=MODEL_HAIKU_45, client=client
            )
            is None
        )


# ---------------------------------------------------------------------------
# Matcher protocol integration
# ---------------------------------------------------------------------------


class TestMatcherIntegration:
    def test_judge_matcher_scores_paraphrased_catch_substring_misses(self) -> None:
        """The motivating case: same findings, judge credits the catch."""
        spec = _spec()
        findings = [_paraphrased_finding()]

        # Substring matcher misses the paraphrase ("water gauge" ≠ "w.g.").
        substring_score = score_spec_review(spec, findings)
        assert substring_score.matched_defect_count == 0

        matches = {0: judge.JudgeMatch(0, 0, "Same contradiction, different words.")}
        matcher = judge.matcher_from_matches(spec, matches, findings)
        judged_score = score_spec_review(spec, findings, matcher=matcher)
        assert judged_score.matched_defect_count == 1
        assert judged_score.severity_match_count == 1

    def test_matcher_returns_none_for_unmatched_defect(self) -> None:
        spec = _spec()
        findings = [_unrelated_finding()]
        matches = {0: judge.JudgeMatch(0, None, "Nothing identifies it.")}
        matcher = judge.matcher_from_matches(spec, matches, findings)
        assert matcher(spec.expected_defects[0], findings) is None

    def test_default_matcher_unchanged(self) -> None:
        """Regression: the hermetic substring path behaves exactly as before."""
        spec = _spec()
        finding = Finding(
            severity="HIGH",
            fileName=spec.filename,
            section="2.01",
            issue="Duct pressure stated as both 2 and 4 inches w.g.",
            actionType="REPORT_ONLY",
            existingText=None,
            replacementText=None,
            codeReference=None,
        )
        assert defect_matched(spec.expected_defects[0], [finding]) is finding
        score = score_spec_review(spec, [finding])
        assert score.matched_defect_count == 1
        assert score.match_method == "substring"
