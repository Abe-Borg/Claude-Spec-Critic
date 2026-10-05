"""Truncated tool-input salvage and bounded, one-shot review recovery."""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from anthropic import not_given
from anthropic.lib.streaming import MessageStream
from anthropic.types import (
    RawContentBlockDeltaEvent, RawContentBlockStartEvent, RawContentBlockStopEvent,
    RawMessageDeltaEvent, RawMessageStartEvent, RawMessageStopEvent,
)
from anthropic.types.messages import MessageBatchIndividualResponse

from src.batch import batch as batch_mod
from src.batch.batch import BatchJob
from src.batch.batch_runtime import PollOutcome
from src.core.api_config import MODEL_HAIKU_45, MODEL_OPUS_5, MODEL_SONNET_46
from src.core.request_budget import InputCount
from src.input.extractor import ExtractedSpec
from src.orchestration import pipeline as pl
from src.review import realtime_review as rt
from src.review import review_request_builder as rb
from src.review.reviewer import merge_review_repair_result, review_result_from_message
from tests.fixtures.fake_anthropic import sample_review_findings_payload


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    monkeypatch.setattr(rb, "count_tokens", lambda text: len(text) // 4)
    monkeypatch.setattr(rt, "review_extended_output_count", lambda spec: 100)
    monkeypatch.setenv("SPEC_CRITIC_UI_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.delenv("SPEC_CRITIC_REVIEW_OUTPUT_CONSTRAINT", raising=False)
    monkeypatch.delenv("SPEC_CRITIC_REVIEW_EFFORT", raising=False)


def _item(issue="First complete issue", severity="HIGH"):
    item = copy.deepcopy(sample_review_findings_payload()["findings"][0])
    item.update(fileName="A.docx", issue=issue, severity=severity, actionType="REPORT_ONLY")
    return item


def _message(items, *, stop="max_tokens"):
    return {
        "id": "msg_test", "type": "message", "role": "assistant", "model": MODEL_OPUS_5,
        "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 200},
        "content": [{"type": "tool_use", "id": "toolu_test",
                     "name": "submit_review_findings", "input": {"findings": items}}],
    }


@pytest.mark.parametrize("as_dict", [False, True])
def test_batch_sdk_retains_complete_input_objects(monkeypatch, as_dict):
    tail = {"severity": "HIGH", "fileName": "A.docx", "issue": "Unfinished issue"}
    envelope = MessageBatchIndividualResponse.model_validate({
        "custom_id": "primary", "result": {"type": "succeeded", "message": _message([_item(), tail])},
    })
    if as_dict:
        rr = review_result_from_message(envelope.result.message.model_dump(), model=MODEL_OPUS_5)
    else:
        monkeypatch.setattr(batch_mod, "_collect_batch_results_with_retry", lambda _: {"primary": envelope})
        job = BatchJob("batch_primary", "review", {"primary": {"filename": "A.docx"}}, 0)
        rr = batch_mod.retrieve_review_results(job, model=MODEL_OPUS_5)["primary"]
    assert [f.issue for f in rr.findings] == ["First complete issue"]
    assert rr.parse_status == "incomplete" and rr.parse_source == "tool"
    assert rr.error and rr.stop_reason == "max_tokens"
    assert (rr.input_tokens, rr.output_tokens, rr.message_id) == (100, 200, "msg_test")


class _RawEvents:
    def __init__(self, events):
        self.events = events

    def __iter__(self):
        return iter(self.events)

    def close(self):
        pass


def _sdk_stream(raw_input, *, stop="max_tokens"):
    start = _message([])
    start.update(content=[], stop_reason=None)
    events = [
        RawMessageStartEvent.model_validate({"type": "message_start", "message": start}),
        RawContentBlockStartEvent.model_validate({"type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_test", "name": "submit_review_findings", "input": {}}}),
        # Several deltas exercise accumulation, including a split inside a string.
        *[RawContentBlockDeltaEvent.model_validate({"type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": part}})
          for part in (raw_input[:30], raw_input[30:70], raw_input[70:])],
        RawContentBlockStopEvent.model_validate({"type": "content_block_stop", "index": 0}),
        RawMessageDeltaEvent.model_validate({"type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 200}}),
        RawMessageStopEvent(type="message_stop"),
    ]
    return MessageStream(_RawEvents(events), output_format=not_given)


def test_stream_sdk_partial_parser_exposes_unclosed_tail_but_salvage_rejects_it():
    first, tail = _item(), _item("All fields arrived but no closing brace")
    raw = '{"findings":[' + json.dumps(first) + ',' + json.dumps(tail)[:-1]
    stream = _sdk_stream(raw)
    list(stream)
    message = stream.get_final_message()
    assert message.content[0].input["findings"] == [first, tail]
    rr = review_result_from_message(message, model=MODEL_OPUS_5, tool_input_json={0: raw})
    assert [f.issue for f in rr.findings] == [first["issue"]]
    assert rr.parse_status == "incomplete"


@pytest.mark.parametrize("stop", ["max_tokens", "model_context_window_exceeded"])
def test_json_arm_salvages_closed_objects_without_fabricating_tail(stop):
    first = _item('Quoted braces } and \\"findings\\": [ inside an issue')
    raw = '{"analysis_summary":"", "findings":[' + json.dumps(first) + ', {"severity":"HIGH"'
    message = _message([], stop=stop)
    message["content"] = [{"type": "text", "text": raw}]
    rr = review_result_from_message(message, model=MODEL_OPUS_5)
    assert [f.issue for f in rr.findings] == [first["issue"]]
    assert rr.parse_status == "incomplete" and rr.parse_source == "json"


@pytest.mark.parametrize("kind", ["thinking", "wrong_tool", "refusal", "stop_sequence"])
def test_other_content_never_becomes_salvaged_findings(kind):
    message = _message([_item()])
    if kind == "thinking":
        message["content"] = [{"type": "thinking", "thinking": "Review not submitted"}]
    elif kind == "wrong_tool":
        message["content"][0]["name"] = "unrelated_tool"
    else:
        message["stop_reason"] = kind
    rr = review_result_from_message(message, model=MODEL_OPUS_5)
    assert not rr.findings and rr.parse_status != "ok"


@pytest.mark.parametrize("model", [MODEL_OPUS_5, MODEL_SONNET_46, MODEL_HAIKU_45])
@pytest.mark.parametrize("mode", ["tool_auto", "forced_tool", "json_schema"])
def test_repair_changes_only_user_suffix_and_effort(monkeypatch, model, mode):
    monkeypatch.setenv("SPEC_CRITIC_REVIEW_OUTPUT_CONSTRAINT", mode)
    monkeypatch.setenv("SPEC_CRITIC_REVIEW_EFFORT", "high")
    spec = rb.ReviewRequestSpec("Spec content", "A.docx", model,
                                project_context="Shared project context", force_allow_extended_output=False)
    primary = rb.build_review_request(spec)
    repair = rb.build_review_request(replace(spec, retry_instruction=rb.RETRY_TRUNCATED_REVIEW_INSTRUCTION))
    before, after = copy.deepcopy(primary.params), copy.deepcopy(repair.params)
    for params in (before, after):
        params.pop("messages")
        if "output_config" in params:
            params["output_config"].pop("effort", None)
    assert before == after
    assert repair.user_message.startswith(primary.user_message)
    expected = rb.RETRY_TRUNCATED_REVIEW_INSTRUCTION_JSON if primary.output_mode == "json_schema" else rb.RETRY_TRUNCATED_REVIEW_INSTRUCTION
    assert repair.user_message.rstrip().endswith(expected)
    assert "at most 20 findings" in expected and "at most 40 words" in expected
    assert "CRITICAL, HIGH, MEDIUM, GRIPES" in expected
    assert "exact evidence/edit quotes" in expected
    if model == MODEL_HAIKU_45:
        assert "effort" not in repair.params.get("output_config", {})
    else:
        assert primary.params["output_config"]["effort"] == "high"
        assert repair.params["output_config"]["effort"] == "low"


def test_repair_suffix_cannot_promote_primary_output_cap(monkeypatch):
    def count(params, **kwargs):
        tokens = rb.LARGE_REVIEW_INPUT_THRESHOLD + (1 if "previously truncated review" in json.dumps(params) else -1)
        return InputCount(tokens=tokens, source="api_estimate")

    monkeypatch.setattr(rb, "resolve_input_count", count)
    spec = rb.ReviewRequestSpec("Spec content", "A.docx", MODEL_OPUS_5)
    primary = rb.build_review_request(spec)
    repair_spec = replace(spec, retry_instruction=rb.RETRY_TRUNCATED_REVIEW_INSTRUCTION)
    repair = rb.build_review_request(repair_spec)
    assert not primary.allow_extended_output and not repair.allow_extended_output
    assert repair.params["max_tokens"] == primary.params["max_tokens"]
    budget = rb.review_request_budget(repair_spec)
    assert budget.count == rb.LARGE_REVIEW_INPUT_THRESHOLD + 1
    assert budget.output_reserve == repair.params["max_tokens"]


def _spec():
    return ExtractedSpec("A.docx", "PART 1 GENERAL. Equipment requirements.", 6)


def _submission():
    job = BatchJob("batch_primary", "review", {"primary": {"filename": "A.docx", "index": 0, "type": "review"}}, 0)
    return pl.BatchSubmission(job=job, review_request_ids=["primary"], files_reviewed=["A.docx"],
                              model=MODEL_OPUS_5, prepared_specs=[_spec()])


def test_batch_two_truncations_merge_findings_and_never_submit_third_attempt(monkeypatch):
    submission = _submission()
    primary = review_result_from_message(_message([_item()]), model=MODEL_OPUS_5)
    repair = review_result_from_message(_message([_item("Second issue", "MEDIUM")]), model=MODEL_OPUS_5)
    repairs = []

    def submit(specs, **kwargs):
        repairs.append(kwargs)
        return BatchJob("batch_repair", "review", {"repair": {"filename": "A.docx"}}, 0)

    monkeypatch.setattr(pl, "submit_review_batch", submit)
    monkeypatch.setattr(pl, "poll_batch_bounded", lambda *_a, **_k: PollOutcome(terminal=True, terminal_status="ended"))
    monkeypatch.setattr(pl, "retrieve_review_results", lambda job, **kw:
                        {"primary": primary} if job.batch_id == "batch_primary" else {"repair": repair})
    first = pl.collect_review_batch_results(submission)
    second = pl.collect_review_batch_results(submission)
    assert len(repairs) == 1 and repairs[0]["retry_instruction"] == rb.RETRY_TRUNCATED_REVIEW_INSTRUCTION
    for state in (first, second):
        assert {f.issue for f in state.review_result.findings} == {"First complete issue", "Second issue"}
        assert state.truncated_specs == ["A.docx"]
        assert "A.docx" in state.review_result.error and "partial" in state.review_result.error
        assert "No findings extracted" not in state.review_result.error
        assert pl.finalize_batch_result(state).failed_review_specs == ["A.docx"]


@pytest.mark.parametrize("saved_repair", [False, True], ids=["fresh", "saved"])
@pytest.mark.parametrize("repair_stop, finding_count, incomplete", [
    ("tool_use", 1, False),
    ("tool_use", 0, False),
    ("max_tokens", 1, True),
    ("max_tokens", 0, True),
    ("tool_use", rb.REVIEW_REPAIR_FINDING_LIMIT, True),
    ("refusal", 0, True),
])
def test_missing_primary_batch_result_can_be_repaired_once(
    monkeypatch, tmp_path, saved_repair, repair_stop, finding_count, incomplete,
):
    monkeypatch.setenv("SPEC_CRITIC_PENDING_BATCH_PATH", str(tmp_path / "pending.json"))
    submission = _submission()
    job = BatchJob("batch_repair", "review", {"repair": {"filename": "A.docx"}}, 0)
    if saved_repair:
        submission.repair_batch_id = job.batch_id
        submission.repair_request_map = job.request_map
        submission.prepared_specs = None  # Resume needs no original input files.
    items = [_item(f"Repaired issue {i}") for i in range(finding_count)]
    repair = review_result_from_message(_message(items, stop=repair_stop), model=MODEL_OPUS_5)
    submissions = []

    def submit(specs, **kwargs):
        submissions.append(job.batch_id)
        return job

    monkeypatch.setattr(pl, "submit_review_batch", submit)
    monkeypatch.setattr(pl, "poll_batch_bounded", lambda *_a, **_k: PollOutcome(terminal=True, terminal_status="ended"))
    monkeypatch.setattr(pl, "retrieve_review_results", lambda batch, **kw:
                        {} if batch.batch_id == "batch_primary" else {"repair": repair})
    first = pl.collect_review_batch_results(submission)
    second = pl.collect_review_batch_results(submission)
    assert submissions == ([] if saved_repair else [job.batch_id])
    for state in (first, second):
        assert state.review_result.findings == repair.findings
        assert state.collection_outcome.repair.recovered == (0 if incomplete else 1)
        assert state.truncated_specs == (["A.docx"] if incomplete else [])
        assert pl.finalize_batch_result(state).failed_review_specs == (["A.docx"] if incomplete else [])
        if incomplete:
            assert "A.docx" in state.review_result.error
        else:
            assert not state.review_result.error


@pytest.mark.parametrize("repair_stop", ["max_tokens", "tool_use"])
def test_realtime_sdk_stream_keeps_paid_findings_with_exactly_one_repair(monkeypatch, repair_stop):
    raw_primary = '{"findings":[' + json.dumps(_item()) + ', {"severity":"HIGH"'
    repair_payload = {"analysis_summary": "", "findings": [_item("Second issue", "MEDIUM")]}
    streams = [_sdk_stream(raw_primary), _sdk_stream(json.dumps(repair_payload), stop=repair_stop)]
    calls = []

    def stream(**kwargs):
        calls.append(kwargs)
        return streams.pop(0)

    monkeypatch.setattr(rt, "_get_client", lambda **kw: SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    results, request_map = rt.run_realtime_review([_spec()], model=MODEL_OPUS_5)
    (rr,) = results.values()
    assert len(calls) == 2 and not streams
    assert {f.issue for f in rr.findings} == {"First complete issue", "Second issue"}
    assert rr.parse_status == ("ok" if repair_stop == "tool_use" else "incomplete")
    assert [a["role"] for a in rr.call_usage] == ["primary", "repair"]
    assert calls[0]["system"] == calls[1]["system"] and calls[0]["tools"] == calls[1]["tools"]
    assert calls[1]["output_config"]["effort"] == "low"
    submission = _submission()
    submission.review_transport = "realtime"
    submission.realtime_results = results
    submission.review_request_ids = list(request_map)
    submission.job.request_map = request_map
    state = pl.collect_review_batch_results(submission)
    assert state.truncated_specs == ([] if repair_stop == "tool_use" else ["A.docx"])


def test_repair_at_count_ceiling_stays_partial_even_with_success_stop():
    primary = review_result_from_message(_message([_item("Primary issue")]), model=MODEL_OPUS_5)
    items = [_item(f"Repair issue {i}") for i in range(rb.REVIEW_REPAIR_FINDING_LIMIT)]
    repair = review_result_from_message(_message(items, stop="tool_use"), model=MODEL_OPUS_5)
    merged = merge_review_repair_result(primary, repair)
    assert len(merged.findings) == 21
    assert merged.parse_status == "incomplete" and "20-finding limit" in merged.error
    assert repair.parse_status == "ok"  # Keep the raw attempt classification for accounting.


def test_bounded_repair_cannot_certify_a_high_volume_primary_complete():
    items = [_item(f"Primary issue {i}") for i in range(25)]
    primary = review_result_from_message(_message(items), model=MODEL_OPUS_5)
    repair = review_result_from_message(_message([_item("Repair issue")], stop="tool_use"), model=MODEL_OPUS_5)
    merged = merge_review_repair_result(primary, repair)
    assert len(merged.findings) == 26
    assert merged.parse_status == "incomplete" and "primary already returned" in merged.error


def test_report_retains_salvage_and_names_incomplete_spec(monkeypatch, tmp_path):
    from docx import Document
    from src.output.report_exporter import export_report

    submission = _submission()
    submission.prepared_specs = None  # An unavailable repair still preserves primary salvage.
    primary = review_result_from_message(_message([_item()]), model=MODEL_OPUS_5)
    monkeypatch.setattr(pl, "retrieve_review_results", lambda *_a, **_k: {"primary": primary})
    result = pl.finalize_batch_result(pl.collect_review_batch_results(submission))
    path = tmp_path / "partial.docx"
    export_report(result, path)
    text = " ".join(Document(path).element.itertext())
    assert "First complete issue" in text
    assert "1 spec failed review" in text and "A.docx" in text
    assert result.failed_review_specs == ["A.docx"]
