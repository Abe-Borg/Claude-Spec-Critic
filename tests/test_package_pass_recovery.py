"""Bounded output recovery preserves package scope, coverage and billed usage."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from src.compliance import compliance_checker as compliance
from src.cross_check import cross_checker as cross
from src.input.extractor import ExtractedSpec
from src.modules import DATACENTER_FIRE
from src.research import RequirementsProfile, ResearchItem
from src.review.structured_schemas import COMPLIANCE_TOOL_NAME, CROSS_CHECK_TOOL_NAME
from tests.fixtures.count_api import count_response, spec_blocks, user_text
from tests.fixtures.fake_anthropic import (
    FakeCacheCreation, FakeMessage, FakeTextBlock, FakeToolUseBlock, FakeUsage,
    sample_compliance_payload,
)


RID = "r-aaaaaaaaaaaa"


def specs(count=4):
    # One CSI group: grouping alone cannot reduce the failing request.
    return [ExtractedSpec(
        filename=f"21 13 {index:02d} Fire.docx", content=f"Provide sprinkler type {index}.",
        word_count=4,
    ) for index in range(count)]


def profile():
    return RequirementsProfile(items=[ResearchItem(
        item_id=RID, dimension_id="governing_codes", topic="Codes",
        category="governing_code", requirement="Provide sprinkler protection.",
        grounded=True, accepted_sources=["https://codes.example.gov/fire"],
    )])


def malformed():
    return FakeMessage(content=[FakeTextBlock(text="garbled response")])


def truncated():
    # Even a complete-looking payload is unusable when the response truncated.
    message = good("compliance")
    message.stop_reason = "max_tokens"
    return message


def good(kind, *, status="missing", addition=False):
    payload = {
        "findings": [], "coordination_summary": "Coordination assessed.",
    } if kind == "cross" else {
        "findings": [], "compliance_summary": "Requirements assessed.",
        "coverage": [{"requirement_id": RID, "status": status,
                      "fileName": "", "section": "", "evidence": "Assessed."}],
    }
    if addition:
        finding = sample_compliance_payload()["findings"][0]
        finding["issue"] = f"Requirement {RID} is missing."
        payload["findings"] = [finding]
    return FakeMessage(
        content=[FakeToolUseBlock(
            name=CROSS_CHECK_TOOL_NAME if kind == "cross" else COMPLIANCE_TOOL_NAME,
            input=payload,
        )], stop_reason="tool_use",
    )


class ScriptedClient:
    def __init__(self, script, *, count_fn=lambda _: 100):
        self.script = iter(script)
        self.calls = []
        self.messages = self
        self.count_fn = count_fn
        self.gate_held = False
        self.permits = 0
        self.held_at_stream = []

    def __enter__(self):
        assert not self.gate_held, "the pass held an outer permit"
        self.gate_held = True
        self.permits += 1
        return self

    def __exit__(self, *_):
        self.gate_held = False

    def count_tokens(self, **kwargs):
        return count_response(self.count_fn(kwargs))

    @contextmanager
    def stream(self, **kwargs):
        self.held_at_stream.append(self.gate_held)
        self.calls.append(kwargs)
        response = next(self.script)
        if isinstance(response, Exception):
            raise response
        yield self.Stream(response)

    class Stream:
        def __init__(self, response):
            self.response = response

        @property
        def text_stream(self):
            return (block.text for block in self.response.content if hasattr(block, "text"))

        def get_final_message(self):
            return self.response


@pytest.fixture
def install(monkeypatch):
    monkeypatch.setattr(compliance, "count_tokens", lambda text: len(text.split()))
    monkeypatch.setattr(cross, "count_tokens", lambda text: len(text.split()))
    monkeypatch.setattr(compliance.time, "sleep", lambda _: None)

    def setup(script, **kwargs):
        client = ScriptedClient(script, **kwargs)
        monkeypatch.setattr(compliance, "_get_client", lambda **_: client)
        monkeypatch.setattr(cross, "_get_client", lambda **_: client)
        return client
    return setup


def run(kind, *, chunked=True, corpus=None, **kwargs):
    module = compliance if kind == "compliance" else cross
    runner = (
        module.run_chunked_compliance_check if chunked else module.run_compliance_check
    ) if kind == "compliance" else (
        module.run_chunked_cross_check if chunked else module.run_cross_check
    )
    args = [corpus if corpus is not None else specs()]
    if kind == "compliance":
        args.append(profile())
    args.append(kwargs.pop("existing_findings", []))
    return runner(*args, cycle=DATACENTER_FIRE.cycle, **kwargs)


def test_compliance_parse_retry_completes_with_full_coverage_and_summed_usage(install):
    first, second = malformed(), good("compliance")
    first.usage = FakeUsage(
        input_tokens=111, output_tokens=22, cache_read_input_tokens=333,
        cache_creation_input_tokens=40,
        cache_creation=FakeCacheCreation(ephemeral_5m_input_tokens=40),
    )
    second.usage = FakeUsage(
        input_tokens=444, output_tokens=55, cache_read_input_tokens=666,
        cache_creation_input_tokens=70,
        cache_creation=FakeCacheCreation(ephemeral_1h_input_tokens=70),
    )
    client = install([first, second])
    result = run("compliance", call_gate=client)
    assert result.cross_check_status == "completed"
    assert result.parse_status == "ok" and result.error is None
    assert result.coverage_completeness.complete
    assert result.coverage[0]["assessment"] == "full"
    assert (result.input_tokens, result.output_tokens) == (555, 77)
    assert result.cache_read_input_tokens == 999
    assert result.cache_creation_input_tokens == 110
    assert result.cache_creation_5m_input_tokens == 40
    assert result.cache_creation_1h_input_tokens == 70
    assert len(client.calls) == 2 and client.calls[0] == client.calls[1]
    assert client.held_at_stream == [True, True]
    assert not client.gate_held


def test_two_malformed_compliance_responses_fail_without_a_third_request(install):
    client = install([malformed(), malformed(), good("compliance")])
    result = run("compliance")
    assert result.cross_check_status == "failed"
    assert result.parse_status == "parse_error"
    assert "Compliance produced no parseable payload" in result.error
    assert not result.coverage_completeness.complete
    assert result.coverage_completeness.unassessed_specs == tuple(s.filename for s in specs())
    assert result.coverage_completeness.omitted_ids == (RID,)
    assert result.coverage == []
    assert (result.input_tokens, result.output_tokens) == (200, 100)
    assert len(client.calls) == 2


@pytest.mark.parametrize("kind", ["compliance", "cross"])
@pytest.mark.parametrize("chunked", [True, False])
def test_max_tokens_recovers_with_forced_split_and_disclosed_scope(install, kind, chunked):
    client = install([truncated(), good(kind), good(kind)])
    result = run(kind, chunked=chunked, call_gate=client)
    assert result.cross_check_status == "completed" and result.error is None
    assert [spec_blocks(call) for call in client.calls] == [4, 2, 2]
    assert (result.input_tokens, result.output_tokens) == (300, 150)
    assert result.chunk_failures == result.chunk_skips == 0
    assert "max_tokens" in result.thinking
    assert "different chunks" in result.thinking
    assert (cross._CHUNK_SUBSET_NOTE in user_text(client.calls[1])
            if kind == "cross" else "subset" in user_text(client.calls[1]).lower())
    assert set(s.filename for s in specs()) == {
        s.filename for s in specs() if any(s.filename in user_text(c) for c in client.calls[1:])
    }
    assert not client.gate_held
    assert client.held_at_stream == [True, True, True]
    if kind == "compliance":
        assert result.coverage_completeness.complete
        assert result.coverage[0]["status"] == "missing"
    else:
        assert [len(entry["files"]) for entry in result.chunk_plan] == [2, 2]


@pytest.mark.parametrize("kind, count", [("compliance", 1), ("cross", 2)])
def test_indivisible_package_keeps_original_failure(install, kind, count):
    client = install([truncated(), good(kind)])
    result = run(kind, corpus=specs(count))
    assert result.cross_check_status == "failed"
    assert result.parse_status == "incomplete" and result.stop_reason == "max_tokens"
    assert len(client.calls) == 1
    assert (result.input_tokens, result.output_tokens) == (100, 50)


@pytest.mark.parametrize("kind", ["compliance", "cross"])
def test_second_truncation_is_terminal_and_counts_failed_chunks(install, kind):
    client = install([truncated(), truncated(), truncated(), good(kind)])
    result = run(kind)
    assert result.cross_check_status == "failed"
    assert result.chunk_failures == 2
    assert result.chunk_skips == 0
    assert len(client.calls) == 3
    assert (result.input_tokens, result.output_tokens) == (300, 150)
    if kind == "compliance":
        assert not result.coverage_completeness.complete
        assert result.coverage_completeness.omitted_ids == (RID,)


@pytest.mark.parametrize("kind", ["compliance", "cross"])
def test_parse_retry_and_truncation_share_one_recovery(install, kind):
    client = install([malformed(), truncated(), good(kind)])
    result = run(kind)
    assert result.cross_check_status == "failed"
    assert result.parse_status == "incomplete"
    assert len(client.calls) == 2
    assert (result.input_tokens, result.output_tokens) == (200, 100)


@pytest.mark.parametrize("kind", ["compliance", "cross"])
def test_forced_split_does_not_allow_a_parse_retry_in_each_chunk(install, kind):
    client = install([truncated(), malformed(), malformed(), good(kind)])
    result = run(kind)
    assert result.cross_check_status == "failed"
    assert result.chunk_failures == 2
    assert len(client.calls) == 3


def test_partial_compliance_recovery_holds_add_and_marks_unassessed_specs(install):
    client = install([truncated(), good("compliance", addition=True), truncated()])
    result = run("compliance")
    assert result.cross_check_status == "completed"
    assert result.chunk_failures == 1 and result.chunk_skips == 0
    assert not result.coverage_completeness.complete
    assert result.coverage_completeness.unassessed_specs == tuple(s.filename for s in specs()[2:])
    assert result.coverage[0]["status"] == "unclear"
    assert result.coverage[0]["assessment"] == "partial"
    assert len(result.findings) == 1
    assert result.findings[0].actionType == "REPORT_ONLY"
    assert result.coverage_completeness.held_addition_count == 1
    assert len(client.calls) == 3


def test_cross_check_stranded_spec_is_disclosed_as_skip(install):
    client = install([truncated(), good("cross")])
    result = run("cross", corpus=specs(3))
    assert result.cross_check_status == "completed"
    assert result.chunk_skips == 1 and result.chunk_failures == 0
    assert specs(3)[2].filename in result.thinking
    assert [spec_blocks(call) for call in client.calls] == [3, 2]
    assert [len(entry["files"]) for entry in result.chunk_plan] == [2, 1]


def test_compliance_recovery_preserves_excluded_spec_gap(install):
    install([truncated(), good("compliance", addition=True), good("compliance")])
    result = run("compliance", excluded_specs=["Excluded.docx"])
    assert result.cross_check_status == "completed"
    assert result.coverage_completeness.unassessed_specs == ("Excluded.docx",)
    assert not result.coverage_completeness.complete
    assert result.coverage[0]["status"] == "unclear"
    assert result.findings[0].actionType == "REPORT_ONLY"


def test_parse_retry_obeys_attempt_budget_and_keeps_coverage_omissions(install):
    client = install([malformed(), good("compliance")])
    result = run("compliance", max_retries=1)
    assert result.cross_check_status == "failed" and len(client.calls) == 1
    response = good("compliance")
    response.content[0].input["coverage"] = []
    install([malformed(), response])
    result = run("compliance")
    assert result.cross_check_status == "completed"
    assert not result.coverage_completeness.complete
    assert result.coverage[0]["assessment"] == "none"


@pytest.mark.parametrize("kind", ["compliance", "cross"])
def test_other_incomplete_stops_do_not_recover(install, kind):
    response = malformed()
    response.stop_reason = "pause_turn"
    client = install([response, good(kind)])
    result = run(kind)
    assert result.cross_check_status == "failed" and len(client.calls) == 1


def test_compliance_transient_retry_does_not_consume_output_recovery(install):
    client = install([RuntimeError("connection reset by peer"), malformed(), good("compliance")])
    result = run("compliance")
    assert result.cross_check_status == "completed" and len(client.calls) == 3
    assert (result.input_tokens, result.output_tokens) == (200, 100)


@pytest.mark.parametrize("kind", ["compliance", "cross"])
def test_initially_chunked_pass_has_one_parse_recovery_across_all_chunks(install, monkeypatch, kind):
    module = compliance if kind == "compliance" else cross
    monkeypatch.setattr(module, "COMPLIANCE_RECOMMENDED_MAX" if kind == "compliance" else "CROSS_CHECK_RECOMMENDED_MAX", 350)
    client = install(
        [malformed(), good(kind), malformed(), good(kind)],
        count_fn=lambda request: spec_blocks(request) * 100,
    )
    # Two divisions force the normal input-driven chunked path.
    corpus = specs()
    for index in range(2, 4):
        corpus[index].filename = f"23 05 {index:02d} HVAC.docx"
    result = run(kind, corpus=corpus)
    assert result.cross_check_status == "completed" and result.chunk_failures == 1
    assert len(client.calls) == 3
    assert (result.input_tokens, result.output_tokens) == (300, 150)


@pytest.mark.parametrize("kind", ["compliance", "cross"])
def test_chunk_recovery_keeps_completed_siblings_without_resending_them(install, monkeypatch, kind):
    module = compliance if kind == "compliance" else cross
    monkeypatch.setattr(module, "COMPLIANCE_RECOMMENDED_MAX" if kind == "compliance" else "CROSS_CHECK_RECOMMENDED_MAX", 350)
    first = good(kind, addition=kind == "compliance")
    if kind == "cross":
        first.content[0].input["findings"] = [{
            "issue": "Kept sibling finding.", "fileName": specs()[0].filename,
            "severity": "HIGH", "actionType": "REPORT_ONLY", "section": "1.2",
        }]
    script = [first, truncated(), good(kind)]
    if kind == "compliance":
        script.append(good(kind))
    script.append(good(kind))
    client = install(script, count_fn=lambda request: spec_blocks(request) * 100)
    result = run(kind, corpus=specs(8))
    assert result.cross_check_status == "completed"
    assert [spec_blocks(call) for call in client.calls] == (
        [3, 3, 2, 1, 2] if kind == "compliance" else [3, 3, 2, 2]
    )
    assert (result.input_tokens, result.output_tokens) == (
        (500, 250) if kind == "compliance" else (400, 200)
    )
    assert len(result.findings) == 1
    assert result.chunk_failures == 0
    assert "max_tokens" in result.thinking
    assert "different chunks" in result.thinking
    if kind == "compliance":
        assert result.coverage_completeness.complete
        # The original ADD is settled once, after all replacement chunks.
        assert result.findings[0].actionType == "ADD"
    else:
        assert result.findings[0].issue == "Kept sibling finding."
        assert result.chunk_skips == 1
        assert [len(entry["files"]) for entry in result.chunk_plan] == [3, 2, 1, 2]


@pytest.mark.parametrize("kind", ["compliance", "cross"])
def test_recovery_usage_keeps_cache_ttl_breakdown_and_unknown_usage(install, kind):
    responses = [truncated(), good(kind), good(kind)]
    responses[0].usage = FakeUsage(
        cache_creation_input_tokens=10,
        cache_creation=FakeCacheCreation(ephemeral_1h_input_tokens=10),
        cache_read_input_tokens=20,
    )
    responses[1].usage = FakeUsage(
        cache_creation_input_tokens=30,
        cache_creation=FakeCacheCreation(ephemeral_5m_input_tokens=30),
        cache_read_input_tokens=40,
    )
    responses[2].usage = FakeUsage(cache_creation_input_tokens=50, cache_read_input_tokens=60)
    install(responses)
    result = run(kind)
    assert result.cross_check_status == "completed"
    assert result.cache_creation_input_tokens == 90
    assert result.cache_creation_5m_input_tokens == 30
    assert result.cache_creation_1h_input_tokens == 10
    assert result.cache_creation_unknown_input_tokens == 50
    assert result.cache_read_input_tokens == 120


@pytest.mark.parametrize("kind", ["compliance", "cross"])
def test_recovery_that_cannot_fit_any_subset_keeps_original_failure(install, monkeypatch, kind):
    module = compliance if kind == "compliance" else cross
    monkeypatch.setattr(module, "COMPLIANCE_RECOMMENDED_MAX" if kind == "compliance" else "CROSS_CHECK_RECOMMENDED_MAX", 350)
    # A subset note can make even a one-spec request exceed the input ceiling.
    client = install([truncated(), good(kind)], count_fn=lambda request:
                     100 if spec_blocks(request) == 4 else 400)
    result = run(kind)
    assert result.cross_check_status == "failed"
    assert result.parse_status == "incomplete" and result.stop_reason == "max_tokens"
    assert len(client.calls) == 1
    assert (result.input_tokens, result.output_tokens) == (100, 50)


@pytest.mark.parametrize("kind", ["compliance", "cross"])
def test_initially_chunked_pass_cannot_recover_a_second_truncated_chunk(install, monkeypatch, kind):
    module = compliance if kind == "compliance" else cross
    monkeypatch.setattr(module, "COMPLIANCE_RECOMMENDED_MAX" if kind == "compliance" else "CROSS_CHECK_RECOMMENDED_MAX", 350)
    script = [truncated(), good(kind)]
    if kind == "compliance":
        script.append(good(kind))
    script.extend([truncated(), good(kind)])
    client = install(script, count_fn=lambda request: spec_blocks(request) * 100)
    result = run(kind, corpus=specs(8))
    assert result.cross_check_status == "completed"
    assert result.chunk_failures == 1
    assert [spec_blocks(call) for call in client.calls] == (
        [3, 2, 1, 3, 2] if kind == "compliance" else [3, 2, 3, 2]
    )
    if kind == "compliance":
        assert not result.coverage_completeness.complete
        assert result.coverage_completeness.unassessed_specs == tuple(s.filename for s in specs(8)[3:6])
