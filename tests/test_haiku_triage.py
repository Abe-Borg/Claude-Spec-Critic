"""Haiku 5.5 triage request compatibility and conservative routing failures."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.api_config import MODEL_HAIKU_45, MODEL_HAIKU_55
from src.review.reviewer import Finding
from src.verification import triage
from tests.fixtures.fake_anthropic import (
    FakeMessage,
    FakeThinkingBlock,
    FakeToolUseBlock,
    FakeUsage,
)


def _finding(*, severity="MEDIUM", code_reference="") -> Finding:
    return Finding(
        severity=severity,
        fileName="23 22 00 - Steam.docx",
        section="2.1",
        issue="Two quoted paragraphs specify different equipment tags.",
        actionType="REPORT_ONLY",
        existingText="Equipment tag P-1.",
        replacementText="Equipment tag P-2.",
        confidence=0.8,
        codeReference=code_reference,
    )


def _response(*, stop_reason="tool_use", payload=None, thinking=False):
    if payload is None:
        payload = {"classifications": [{"index": 0, "classification": "local_skip"}]}
    content = [FakeToolUseBlock(name=triage.TRIAGE_TOOL_NAME, input=payload)]
    if thinking:
        content.insert(0, FakeThinkingBlock())
    return FakeMessage(
        content=content,
        stop_reason=stop_reason,
        usage=FakeUsage(input_tokens=900, output_tokens=140),
    )


def _client(monkeypatch, response):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return response

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        triage, "_get_client", lambda **_: SimpleNamespace(messages=SimpleNamespace(create=create))
    )
    return calls


def test_default_triage_uses_haiku_55_with_supported_parameters(monkeypatch):
    monkeypatch.delenv("SPEC_CRITIC_STRICT_TOOL_USE", raising=False)
    calls = _client(monkeypatch, _response())
    assert triage.classify_findings_with_haiku([_finding()]) == {0: "local_skip"}
    request = calls[0]
    assert request["model"] == MODEL_HAIKU_55
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": "medium"}
    assert request["max_tokens"] == 16_000
    assert request["tool_choice"] == {
        "type": "tool", "name": triage.TRIAGE_TOOL_NAME, "disable_parallel_tool_use": True
    }
    assert request["tools"][0]["strict"] is True
    assert request["messages"][-1]["role"] == "user"
    assert not {"temperature", "top_p", "top_k", "budget_tokens"} & request.keys()
    assert "budget_tokens" not in request["thinking"]


def test_pinned_haiku_45_retains_legacy_request(monkeypatch):
    calls = _client(monkeypatch, _response())
    assert triage.classify_findings_with_haiku([_finding()], model=MODEL_HAIKU_45) == {
        0: "local_skip"
    }
    assert calls[0]["max_tokens"] == 8_000
    assert "thinking" not in calls[0]
    assert "output_config" not in calls[0]
    assert calls[0]["tool_choice"]["type"] == "tool"


def test_unknown_model_preserves_safe_request_shape(monkeypatch):
    calls = _client(monkeypatch, _response())
    triage.classify_findings_with_haiku([_finding()], model="claude-unknown-9")
    assert "thinking" not in calls[0]
    assert "output_config" not in calls[0]
    assert "strict" not in calls[0]["tools"][0]
    assert calls[0]["tool_choice"]["type"] == "auto"


def test_tool_parser_selects_by_type_after_thinking(monkeypatch):
    _client(monkeypatch, _response(thinking=True))
    assert triage.classify_findings_with_haiku([_finding()]) == {0: "local_skip"}


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal", "pause_turn", "stop_sequence", None])
def test_incomplete_or_refused_chunk_never_accepts_partial_skips(monkeypatch, stop_reason):
    response = _response(stop_reason=stop_reason)
    calls = _client(monkeypatch, response)
    usage, logs = [], []
    assert triage.classify_findings_with_haiku(
        [_finding()], usage_sink=usage.append, log=lambda msg, **_: logs.append(msg)
    ) == {}
    assert len(calls) == 1  # A response refusal/truncation isn't retried.
    assert len(usage) == 1
    assert usage[0].input_tokens == 900
    assert usage[0].output_tokens == 140
    assert any("falling back to web_required" in message for message in logs)


@pytest.mark.parametrize("payload", [{}, {"classifications": "invalid"}, {"classifications": [None]}])
def test_unreadable_classifications_fall_back_to_web(monkeypatch, payload):
    _client(monkeypatch, _response(payload=payload))
    assert triage.classify_findings_with_haiku([_finding()]) == {}


def test_higher_severities_and_code_references_never_reach_haiku(monkeypatch):
    calls = _client(monkeypatch, _response(payload={
        "classifications": [{"index": i, "classification": "local_skip"} for i in range(4)]
    }))
    findings = [
        _finding(severity="CRITICAL"), _finding(severity="HIGH"),
        _finding(code_reference="NFPA 13"), _finding(),
    ]
    assert triage.classify_findings_with_haiku(findings) == {3: "local_skip"}
    assert len(calls) == 1
    prompt = calls[0]["messages"][0]["content"]
    assert '<finding index="3">' in prompt
    assert all(f'<finding index="{i}">' not in prompt for i in range(3))
    assert list(triage.filter_local_skips(findings, {i: "local_skip" for i in range(4)})) == [3]


def test_missing_or_unsent_indices_cannot_skip_findings(monkeypatch):
    _client(monkeypatch, _response(payload={"classifications": [
        {"index": 0, "classification": "local_skip"},
        {"index": 500, "classification": "local_skip"},
    ]}))
    assert triage.classify_findings_with_haiku([_finding(), _finding()]) == {0: "local_skip"}
