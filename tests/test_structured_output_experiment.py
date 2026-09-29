"""EX-02 (plan S21): the review output-constraint experiment, offline.

The switch ``SPEC_CRITIC_REVIEW_OUTPUT_CONSTRAINT`` builds the per-spec review
in one of three shapes: ``tool_auto`` (the default), ``forced_tool``, and
``json_schema``. These tests pin, without any API call:

* the switch fails closed, and a model the capability record does not vouch
  for keeps the default shape;
* off is the default request, and ``forced_tool`` changes ``tool_choice`` and
  nothing else — three mechanisms, kept apart;
* ``json_schema`` sends no tool, merges the format into ``output_config``
  (effort kept), counts it, and words the prompts and the retry for it;
* the reader decides by what a response contains, so valid constrained
  output, legacy tool output, the text fallback, a refusal, a truncation, and
  missing content each classify as they should, and schema-valid output still
  goes through the same field validation;
* a batch submitted under one shape is collected under another;
* each review attempt records where its findings came from, and the
  diagnostics summary rolls it up for :mod:`evals.structured_outputs`.
"""
from __future__ import annotations

import copy
import json
import logging
from types import SimpleNamespace

import pytest

from evals import structured_outputs as eval_so
from src.batch import batch as batch_mod
from src.core import api_config
from src.core.api_config import (
    MODEL_HAIKU_45,
    MODEL_OPUS_48,
    MODEL_OPUS_5,
    MODEL_SONNET_46,
    MODEL_SONNET_5,
    PHASE_REVIEW,
    model_capabilities,
)
from src.core.attempt_usage import (
    OPERATION_REVIEW,
    TRANSPORT_BATCH,
    AttemptUsage,
    known_attempt,
    unknown_attempt,
)
from src.core.code_cycles import DEFAULT_CYCLE
from src.core.request_budget import (
    JSON_OUTPUT_SYSTEM_PROMPT_ALLOWANCE,
    TOOL_USE_SYSTEM_PROMPT_ALLOWANCE,
    count_request_from_params,
    local_request_tokens,
)
from src.core import tokenizer
from src.input.extractor import ExtractedSpec
from src.modules.registry import AVAILABLE_MODULES
from src.orchestration import pipeline as pl
from src.orchestration.diagnostics import DiagnosticsReport
from src.review import prompts
from src.review import realtime_review as rt
from src.review import structured_schemas as ss
from src.review.review_request_builder import (
    RETRY_TRUNCATED_REVIEW_INSTRUCTION,
    RETRY_TRUNCATED_REVIEW_INSTRUCTION_JSON,
    ReviewRequestSpec,
    build_review_request,
    build_token_count_request,
)
from src.review.reviewer import (
    PARSE_SOURCE_JSON,
    PARSE_SOURCE_TEXT,
    PARSE_SOURCE_TOOL,
    REPAIRABLE_PARSE_STATUSES,
    review_result_from_message,
)
from tests.fixtures import batch_service as bs
from tests.fixtures.fake_anthropic import (
    FakeBatchResult,
    FakeBatchResultEnvelope,
    FakeMessage,
    FakeTextBlock,
    FakeThinkingBlock,
    max_tokens_incomplete_response,
    review_json_output_response,
    review_tool_use_response,
    sample_review_findings_payload,
)

ENV = ss.ENV_REVIEW_OUTPUT_CONSTRAINT
DOCUMENTED_MODELS = (MODEL_OPUS_5, MODEL_OPUS_48, MODEL_SONNET_5, MODEL_SONNET_46)
_MODULES = list(AVAILABLE_MODULES.values()) if isinstance(AVAILABLE_MODULES, dict) else list(AVAILABLE_MODULES)


@pytest.fixture(autouse=True)
def _clean_switch(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setattr(ss, "_WARNED_REVIEW_OUTPUT_VALUES", set())
    monkeypatch.setattr(ss, "_WARNED_REVIEW_OUTPUT_UNSUPPORTED", set())
    # Hermetic sizing: the extended-output decision pads a local count, and
    # the container cannot load the rank file.
    monkeypatch.setattr(
        "src.review.review_request_builder.count_tokens", lambda text: len(text) // 4
    )


def _spec(*, model: str = MODEL_OPUS_5, cycle=DEFAULT_CYCLE, retry: str | None = None, context: str = "") -> ReviewRequestSpec:
    return ReviewRequestSpec(
        spec_content="PART 1 GENERAL\n1.01 SUMMARY\nA. Provide the specified piping system.",
        filename="230500.docx",
        model=model,
        cycle=cycle,
        project_context=context,
        retry_instruction=retry,
    )


def _dumps(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


# ===========================================================================
# 1. The switch and its capability gate
# ===========================================================================


class TestSwitch:
    @pytest.mark.parametrize("value", [None, "", "  ", "0", "false", "No", "OFF"])
    def test_off_values_mean_the_default(self, monkeypatch, value):
        if value is not None:
            monkeypatch.setenv(ENV, value)
        assert ss.requested_review_output_constraint() == ss.REVIEW_OUTPUT_TOOL_AUTO

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("forced_tool", ss.REVIEW_OUTPUT_FORCED_TOOL),
            (" JSON_SCHEMA ", ss.REVIEW_OUTPUT_JSON_SCHEMA),
            ("Forced_Tool", ss.REVIEW_OUTPUT_FORCED_TOOL),
        ],
    )
    def test_recognized_values(self, monkeypatch, value, expected):
        monkeypatch.setenv(ENV, value)
        assert ss.requested_review_output_constraint() == expected

    @pytest.mark.parametrize("value", ["tool_auto", "json", "forced", "1", "on", "strict"])
    def test_anything_else_fails_closed_with_one_warning(self, monkeypatch, caplog, value):
        monkeypatch.setenv(ENV, value)
        with caplog.at_level(logging.WARNING, logger=ss.__name__):
            assert ss.requested_review_output_constraint() == ss.REVIEW_OUTPUT_TOOL_AUTO
            assert ss.requested_review_output_constraint() == ss.REVIEW_OUTPUT_TOOL_AUTO
        warnings = [r for r in caplog.records if ENV in r.getMessage()]
        assert len(warnings) == 1

    @pytest.mark.parametrize("model", DOCUMENTED_MODELS)
    def test_forced_tool_with_thinking_on_documented_models(self, monkeypatch, model):
        monkeypatch.setenv(ENV, "forced_tool")
        assert ss.review_output_mode(model=model, thinking=True) == ss.REVIEW_OUTPUT_FORCED_TOOL

    def test_forced_tool_on_haiku_only_without_thinking(self, monkeypatch, caplog):
        # Haiku has no adaptive thinking; forcing without thinking is the
        # shape triage already sends.
        monkeypatch.setenv(ENV, "forced_tool")
        assert ss.review_output_mode(model=MODEL_HAIKU_45, thinking=False) == ss.REVIEW_OUTPUT_FORCED_TOOL
        with caplog.at_level(logging.WARNING, logger=ss.__name__):
            assert ss.review_output_mode(model=MODEL_HAIKU_45, thinking=True) == ss.REVIEW_OUTPUT_TOOL_AUTO
        assert any("with thinking" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("model", (*DOCUMENTED_MODELS, MODEL_HAIKU_45))
    def test_json_schema_on_documented_models(self, monkeypatch, model):
        monkeypatch.setenv(ENV, "json_schema")
        assert ss.review_output_mode(model=model, thinking=True) == ss.REVIEW_OUTPUT_JSON_SCHEMA

    @pytest.mark.parametrize("value", ["forced_tool", "json_schema"])
    @pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-unknown-9", None])
    def test_unsupported_capability_selection_keeps_the_default(self, monkeypatch, caplog, value, model):
        # Opus 5.5 is not in the whitelist (and rejects forced tool use
        # outright): an override to it must never turn the experiment into a 400.
        monkeypatch.setenv(ENV, value)
        with caplog.at_level(logging.WARNING, logger=ss.__name__):
            assert ss.review_output_mode(model=model, thinking=True) == ss.REVIEW_OUTPUT_TOOL_AUTO
            assert ss.review_output_mode(model=model, thinking=True) == ss.REVIEW_OUTPUT_TOOL_AUTO
        assert len([r for r in caplog.records if "not documented" in r.getMessage()]) == 1

    def test_capability_records(self):
        forced = {m for m, c in api_config._MODEL_CAPABILITIES.items() if c.supports_forced_tool_with_thinking}
        fmt = {m for m, c in api_config._MODEL_CAPABILITIES.items() if c.supports_json_output_format}
        assert forced == set(DOCUMENTED_MODELS)
        assert fmt == {*DOCUMENTED_MODELS, MODEL_HAIKU_45}
        unknown = model_capabilities("claude-unknown-9")
        assert not unknown.supports_forced_tool_with_thinking
        assert not unknown.supports_json_output_format
        # The triage flag is untouched: still Haiku only.
        assert {m for m, c in api_config._MODEL_CAPABILITIES.items() if c.supports_forced_tool_choice} == {MODEL_HAIKU_45}


# ===========================================================================
# 2. Request shapes
# ===========================================================================


class TestRequestShapes:
    @pytest.mark.parametrize("off", ["", "0", "off", "not-a-mode"])
    def test_off_is_the_unswitched_request(self, monkeypatch, off):
        baseline = build_review_request(_spec())
        monkeypatch.setenv(ENV, off)
        switched = build_review_request(_spec())
        assert _dumps(switched.params) == _dumps(baseline.params)
        assert switched.output_mode == baseline.output_mode == ss.REVIEW_OUTPUT_TOOL_AUTO

    def test_default_shape(self):
        built = build_review_request(_spec())
        params = built.params
        assert params["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
        assert [t["name"] for t in params["tools"]] == [ss.REVIEW_TOOL_NAME]
        assert "format" not in params.get("output_config", {})
        assert built.system_prompt == prompts.get_system_prompt(DEFAULT_CYCLE)
        assert "output_config" not in count_request_from_params(params)

    @pytest.mark.parametrize("module", _MODULES, ids=lambda m: m.module_id)
    def test_forced_tool_changes_only_tool_choice(self, monkeypatch, module):
        baseline = build_review_request(_spec(cycle=module.cycle)).params
        monkeypatch.setenv(ENV, "forced_tool")
        forced = build_review_request(_spec(cycle=module.cycle)).params
        changed = {k for k in set(baseline) | set(forced) if baseline.get(k) != forced.get(k)}
        assert changed == {"tool_choice"}
        assert forced["tool_choice"] == {
            "type": "tool",
            "name": ss.REVIEW_TOOL_NAME,
            "disable_parallel_tool_use": True,
        }
        # Strict tool arguments stay exactly as they were: a separate mechanism.
        assert forced["tools"] == baseline["tools"]

    def test_json_schema_shape(self, monkeypatch):
        baseline = build_review_request(_spec()).params
        monkeypatch.setenv(ENV, "json_schema")
        built = build_review_request(_spec())
        params = built.params
        assert built.output_mode == ss.REVIEW_OUTPUT_JSON_SCHEMA
        assert "tools" not in params and "tool_choice" not in params
        assert params["output_config"] == {
            **baseline["output_config"],
            "format": {"type": "json_schema", "schema": ss.REVIEW_FINDINGS_SCHEMA},
        }
        assert params["output_config"]["effort"] == baseline["output_config"]["effort"]
        # Thinking, model, cap, tier: unchanged.
        for key in ("model", "thinking", "max_tokens", "service_tier"):
            assert params.get(key) == baseline.get(key)

    def test_json_schema_on_a_model_without_effort_carries_format_only(self, monkeypatch):
        monkeypatch.setenv(ENV, "json_schema")
        params = build_review_request(_spec(model=MODEL_HAIKU_45)).params
        assert params["output_config"] == {
            "format": {"type": "json_schema", "schema": ss.REVIEW_FINDINGS_SCHEMA}
        }

    def test_the_request_never_aliases_the_module_schema(self, monkeypatch):
        before = copy.deepcopy(ss.REVIEW_FINDINGS_SCHEMA)
        monkeypatch.setenv(ENV, "json_schema")
        params = build_review_request(_spec()).params
        params["output_config"]["format"]["schema"]["required"].append("mutated")
        assert ss.REVIEW_FINDINGS_SCHEMA == before

    def test_json_schema_prompts_change_only_the_lines_that_name_the_tool(self, monkeypatch):
        base = build_review_request(_spec(context="Project: X."))
        monkeypatch.setenv(ENV, "json_schema")
        arm = build_review_request(_spec(context="Project: X."))
        assert "submit_review_findings" not in arm.system_prompt
        assert "findings_json" not in arm.system_prompt
        assert "submit_review_findings" not in arm.user_message
        base_lines, arm_lines = base.user_message.splitlines(), arm.user_message.splitlines()
        assert len(base_lines) == len(arm_lines)
        diff = [(a, b) for a, b in zip(base_lines, arm_lines) if a != b]
        assert diff == [
            (
                "- Submit findings via the submit_review_findings tool.",
                "- Return findings in the JSON object your response format defines.",
            ),
            (
                "- Submit findings once via the submit_review_findings tool. Do not call it twice.",
                "- Return that JSON object once, as your whole final response.",
            ),
        ]
        # The system prompt differs only inside <output>.
        def outside(text):
            start, end = text.index("<output>"), text.index("</output>")
            return text[:start] + text[end:]
        assert outside(arm.system_prompt) == outside(base.system_prompt)

    @pytest.mark.parametrize("module", _MODULES, ids=lambda m: m.module_id)
    def test_default_and_forced_prompts_are_the_default_text(self, module):
        default = prompts.get_system_prompt(module.cycle)
        assert prompts.get_system_prompt(module.cycle, output_mode=ss.REVIEW_OUTPUT_FORCED_TOOL) == default
        assert "submit_review_findings" in default and "<findings_json>" in default

    def test_retry_instruction_is_worded_for_the_request_it_joins(self, monkeypatch):
        tool_repair = build_review_request(_spec(retry=RETRY_TRUNCATED_REVIEW_INSTRUCTION))
        assert tool_repair.user_message.rstrip().endswith(RETRY_TRUNCATED_REVIEW_INSTRUCTION)
        assert RETRY_TRUNCATED_REVIEW_INSTRUCTION_JSON not in tool_repair.user_message
        monkeypatch.setenv(ENV, "json_schema")
        json_repair = build_review_request(_spec(retry=RETRY_TRUNCATED_REVIEW_INSTRUCTION))
        assert RETRY_TRUNCATED_REVIEW_INSTRUCTION_JSON in json_repair.user_message
        assert RETRY_TRUNCATED_REVIEW_INSTRUCTION not in json_repair.user_message
        # A caller-supplied instruction that is not the shared one is kept as is.
        custom = build_review_request(_spec(retry="Custom retry."))
        assert custom.user_message.rstrip().endswith("Custom retry.")

    def test_unsupported_model_builds_the_default_shape(self, monkeypatch):
        baseline = build_review_request(_spec(model="claude-unknown-9")).params
        monkeypatch.setenv(ENV, "json_schema")
        switched = build_review_request(_spec(model="claude-unknown-9"))
        assert switched.output_mode == ss.REVIEW_OUTPUT_TOOL_AUTO
        assert _dumps(switched.params) == _dumps(baseline)


class TestCounting:
    def test_count_form_carries_the_format_only_when_constrained(self, monkeypatch):
        monkeypatch.setenv(ENV, "json_schema")
        built, form = build_token_count_request(_spec())
        assert form["output_config"] == {"format": built.params["output_config"]["format"]}
        assert "effort" not in form["output_config"]
        assert "tools" not in form and "tool_choice" not in form

    def test_local_estimate_counts_the_format_and_its_allowance(self):
        fmt = ss.review_json_output_format()
        form = {"system": "S", "messages": [{"role": "user", "content": "U"}], "output_config": {"format": fmt}}
        counter = lambda text: len(text)  # noqa: E731
        with_format = local_request_tokens(form, counter=counter)
        without = local_request_tokens({k: v for k, v in form.items() if k != "output_config"}, counter=counter)
        assert with_format - without == len(json.dumps(fmt, sort_keys=True, ensure_ascii=False)) + JSON_OUTPUT_SYSTEM_PROMPT_ALLOWANCE
        assert JSON_OUTPUT_SYSTEM_PROMPT_ALLOWANCE == TOOL_USE_SYSTEM_PROMPT_ALLOWANCE

    def test_count_endpoint_receives_output_config(self):
        seen = {}

        def count_tokens(**kwargs):
            seen.update(kwargs)
            return SimpleNamespace(input_tokens=1234)

        client = SimpleNamespace(messages=SimpleNamespace(count_tokens=count_tokens))
        fmt = {"format": ss.review_json_output_format()}
        result = tokenizer.count_input_tokens(
            model=MODEL_OPUS_5, messages=[{"role": "user", "content": "x"}], output_config=fmt, client=client
        )
        assert result.tokens == 1234
        assert seen["output_config"] == fmt

    def test_count_endpoint_call_without_format_is_unchanged(self):
        seen = {}

        def count_tokens(**kwargs):
            seen.update(kwargs)
            return SimpleNamespace(input_tokens=5)

        client = SimpleNamespace(messages=SimpleNamespace(count_tokens=count_tokens))
        tokenizer.count_input_tokens(model=MODEL_OPUS_5, messages=[{"role": "user", "content": "x"}], client=client)
        assert "output_config" not in seen


# ===========================================================================
# 3. The reader: what the response contains decides
# ===========================================================================


def _tool_findings():
    return review_result_from_message(review_tool_use_response(), model=MODEL_OPUS_5).findings


class TestReader:
    @pytest.mark.parametrize("switch", [None, "forced_tool", "json_schema"])
    def test_valid_constrained_output(self, monkeypatch, switch):
        if switch:
            monkeypatch.setenv(ENV, switch)
        payload = sample_review_findings_payload()
        rr = review_result_from_message(review_json_output_response(payload=payload), model=MODEL_OPUS_5)
        assert rr.parse_status == "ok" and rr.error is None
        assert rr.parse_source == PARSE_SOURCE_JSON
        assert rr.thinking == payload["analysis_summary"]
        assert rr.structured_payload == payload
        # The same payload through the tool reads to the same findings.
        assert rr.findings == _tool_findings()

    @pytest.mark.parametrize("switch", [None, "forced_tool", "json_schema"])
    def test_legacy_tool_output(self, monkeypatch, switch):
        if switch:
            monkeypatch.setenv(ENV, switch)
        rr = review_result_from_message(review_tool_use_response(), model=MODEL_OPUS_5)
        assert (rr.parse_status, rr.parse_source) == ("ok", PARSE_SOURCE_TOOL)
        assert len(rr.findings) == 1

    def test_tagged_text_fallback(self):
        items = sample_review_findings_payload()["findings"]
        message = FakeMessage(
            content=[FakeTextBlock(text=f"Notes.\n<findings_json>{json.dumps(items)}</findings_json>")]
        )
        rr = review_result_from_message(message, model=MODEL_OPUS_5)
        assert (rr.parse_status, rr.parse_source) == ("ok", PARSE_SOURCE_TEXT)
        assert rr.findings == _tool_findings()

    def test_zero_findings_is_a_valid_review(self):
        rr = review_result_from_message(
            review_json_output_response(payload={"analysis_summary": "", "findings": []}), model=MODEL_OPUS_5
        )
        assert (rr.parse_status, rr.parse_source, rr.findings) == ("ok", PARSE_SOURCE_JSON, [])

    def test_refusal(self):
        message = FakeMessage(
            content=[FakeTextBlock(text="I can't help with that.")], stop_reason="refusal"
        )
        rr = review_result_from_message(message, model=MODEL_OPUS_5)
        assert rr.parse_status == "refusal" and rr.parse_source == ""
        assert rr.parse_status not in REPAIRABLE_PARSE_STATUSES

    def test_truncated_constrained_output(self):
        body = json.dumps(sample_review_findings_payload())[:60]
        rr = review_result_from_message(
            review_json_output_response(text=body, stop_reason="max_tokens"), model=MODEL_OPUS_5
        )
        assert rr.parse_status == "incomplete" and rr.parse_source == ""
        assert rr.parse_status in REPAIRABLE_PARSE_STATUSES
        assert "truncated" in rr.error

    @pytest.mark.parametrize(
        "content",
        [
            [],
            [FakeThinkingBlock()],
            [FakeThinkingBlock(), FakeTextBlock(text="")],
            [FakeTextBlock(text="I reviewed the spec and found issues.")],
        ],
        ids=["no-content", "thinking-only", "empty-text", "prose"],
    )
    def test_missing_content_is_a_parse_error(self, content):
        rr = review_result_from_message(FakeMessage(content=content), model=MODEL_OPUS_5)
        assert rr.parse_status == "parse_error" and rr.parse_source == ""
        assert rr.parse_status in REPAIRABLE_PARSE_STATUSES

    @pytest.mark.parametrize(
        "body",
        [
            json.dumps({"analysis_summary": "x"}),
            json.dumps({"analysis_summary": "x", "findings": "none"}),
            json.dumps({"findings": None}),
        ],
    )
    def test_an_object_without_a_findings_list_is_not_a_review(self, body):
        rr = review_result_from_message(review_json_output_response(text=body), model=MODEL_OPUS_5)
        assert rr.parse_status == "parse_error"

    def test_prose_around_json_is_not_a_constrained_response(self):
        payload = sample_review_findings_payload()
        rr = review_result_from_message(
            review_json_output_response(text="Here is my review: " + json.dumps(payload)), model=MODEL_OPUS_5
        )
        # Not claimed as a constrained response; the old fallback reads it as before.
        assert rr.parse_source != PARSE_SOURCE_JSON

    def test_schema_valid_output_is_still_field_validated(self):
        payload = {
            "analysis_summary": "",
            "findings": [
                # EDIT without existingText: schema-valid (nullable), unsafe as an edit.
                {**sample_review_findings_payload()["findings"][0], "existingText": None},
                # An unknown severity is dropped, as on the tool path.
                {**sample_review_findings_payload()["findings"][0], "severity": "URGENT"},
            ],
        }
        json_rr = review_result_from_message(review_json_output_response(payload=payload), model=MODEL_OPUS_5)
        tool_rr = review_result_from_message(review_tool_use_response(payload=payload), model=MODEL_OPUS_5)
        assert json_rr.findings == tool_rr.findings
        assert len(json_rr.findings) == 1
        finding = json_rr.findings[0]
        assert finding.actionType == "REPORT_ONLY" and finding.demotion_reason
        assert finding.as_edit_proposal() is None


# ===========================================================================
# 4. Both transports
# ===========================================================================


class _FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @property
    def text_stream(self):
        return iter(())

    def get_final_message(self):
        return self._message


class _FakeRealtimeClient:
    def __init__(self, responses):
        self.calls: list[dict] = []
        self._responses = list(responses)
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeStream(self._responses.pop(0))


def _extracted(name="230500.docx"):
    body = "PART 1 GENERAL\n1.01 SUMMARY\nA. Provide the specified piping system."
    return ExtractedSpec(filename=name, content=body, word_count=len(body.split()))


class TestRealtimeTransport:
    def test_json_arm_end_to_end_with_inline_repair(self, monkeypatch):
        monkeypatch.setenv(ENV, "json_schema")
        monkeypatch.setattr(rt, "review_extended_output_count", lambda spec: 10)
        truncated = review_json_output_response(text='{"analysis_summary": "", "find', stop_reason="max_tokens")
        repaired = review_json_output_response()
        client = _FakeRealtimeClient([truncated, repaired])
        monkeypatch.setattr(rt, "_get_client", lambda **_: client)

        results, _ = rt.run_realtime_review([_extracted()], model=MODEL_OPUS_5)

        (rr,) = results.values()
        assert rr.parse_status == "ok" and rr.parse_source == PARSE_SOURCE_JSON
        assert len(rr.findings) == 1
        assert len(client.calls) == 2
        for call in client.calls:
            assert "tools" not in call
            assert call["output_config"]["format"]["type"] == "json_schema"
        repair_text = client.calls[1]["messages"][0]["content"]
        assert RETRY_TRUNCATED_REVIEW_INSTRUCTION_JSON in repair_text
        # Each call's attempt record says what came back.
        channels = [a["output_channel"] for a in rr.call_usage]
        outcomes = [a["outcome"] for a in rr.call_usage]
        assert outcomes == ["incomplete", "ok"]
        assert channels == ["", PARSE_SOURCE_JSON]

    def test_forced_arm_sends_the_forced_choice(self, monkeypatch):
        monkeypatch.setenv(ENV, "forced_tool")
        monkeypatch.setattr(rt, "review_extended_output_count", lambda spec: 10)
        client = _FakeRealtimeClient([review_tool_use_response()])
        monkeypatch.setattr(rt, "_get_client", lambda **_: client)
        results, _ = rt.run_realtime_review([_extracted()], model=MODEL_OPUS_5)
        (rr,) = results.values()
        assert client.calls[0]["tool_choice"]["type"] == "tool"
        assert rr.parse_source == PARSE_SOURCE_TOOL
        assert rr.call_usage[0]["output_channel"] == PARSE_SOURCE_TOOL


class _FakeBatches:
    def __init__(self):
        self.requests = None

    def create(self, *, requests, **_kwargs):
        self.requests = requests
        return SimpleNamespace(id="msgbatch_FAKE")


class TestBatchTransport:
    @pytest.mark.parametrize(
        "switch, expect_tools, expect_format",
        [(None, True, False), ("forced_tool", True, False), ("json_schema", False, True)],
    )
    def test_submitted_params(self, monkeypatch, switch, expect_tools, expect_format):
        if switch:
            monkeypatch.setenv(ENV, switch)
        batches = _FakeBatches()
        client = SimpleNamespace(messages=SimpleNamespace(batches=batches))
        monkeypatch.setattr(batch_mod, "_get_client", lambda **_: client)
        batch_mod.submit_review_batch([_extracted()], model=MODEL_OPUS_5)
        (request,) = batches.requests
        params = request["params"]
        assert ("tools" in params) is expect_tools
        assert ("format" in params.get("output_config", {})) is expect_format


def _batch_result(custom_id, message):
    return FakeBatchResult(custom_id=custom_id, result=FakeBatchResultEnvelope(type="succeeded", message=message))


class TestSavedBatchesAcrossASwitchChange:
    """A batch submitted under one shape is collected under any other."""

    @pytest.mark.parametrize(
        "submitted_as, collected_under, expected_source",
        [
            ("tool", "json_schema", PARSE_SOURCE_TOOL),
            ("tool", "forced_tool", PARSE_SOURCE_TOOL),
            ("json", None, PARSE_SOURCE_JSON),
            ("json", "forced_tool", PARSE_SOURCE_JSON),
            ("text", "json_schema", PARSE_SOURCE_TEXT),
        ],
    )
    def test_collect(self, monkeypatch, submitted_as, collected_under, expected_source):
        names = ["230500.docx", "230593.docx"]
        sub = bs.submission(names, cross_check=False)
        messages = {
            "tool": review_tool_use_response,
            "json": review_json_output_response,
            "text": lambda: FakeMessage(
                content=[
                    FakeTextBlock(
                        text="<findings_json>"
                        + json.dumps(sample_review_findings_payload()["findings"])
                        + "</findings_json>"
                    )
                ]
            ),
        }[submitted_as]
        results = {cid: _batch_result(cid, messages()) for cid in sub.review_request_ids}
        bs.FakeBatchService(monkeypatch, primary={})
        # The real reader, fed the batch's results as the API returns them.
        monkeypatch.setattr(pl, "retrieve_review_results", batch_mod.retrieve_review_results)
        monkeypatch.setattr(batch_mod, "_collect_batch_results_with_retry", lambda batch_id: dict(results))
        if collected_under:
            monkeypatch.setenv(ENV, collected_under)

        state = pl.collect_review_batch_results(sub)

        assert not state.truncated_specs
        assert state.collection_outcome.repair.state == "not_needed"
        assert state.review_result.findings
        channels = {a["output_channel"] for a in state.review_result.call_usage}
        assert channels == {expected_source}
        assert {a["outcome"] for a in state.review_result.call_usage} == {"ok"}


# ===========================================================================
# 5. Telemetry: attempt records and the diagnostics rollup
# ===========================================================================


class TestAttemptChannel:
    def test_round_trip(self):
        attempt = known_attempt(
            {"input_tokens": 3}, operation=OPERATION_REVIEW, transport=TRANSPORT_BATCH,
            batch_id="b", custom_id="c", outcome="ok", output_channel="json",
        )
        data = attempt.to_dict()
        assert data["output_channel"] == "json"
        assert AttemptUsage.from_dict(data).output_channel == "json"

    def test_a_record_written_before_the_field_reads_as_unrecorded(self):
        data = known_attempt({"input_tokens": 3}, operation=OPERATION_REVIEW, transport=TRANSPORT_BATCH).to_dict()
        data.pop("output_channel")
        assert AttemptUsage.from_dict(data).output_channel == ""


def _review_event(diag, attempts):
    diag.record_api_call(
        phase="batch_collect", model=MODEL_OPUS_5, mode="batch", operation=OPERATION_REVIEW,
        attempts=[a for a in attempts],
    )


class TestDiagnosticsRollup:
    def _attempt(self, cid, outcome, channel="", *, known=True):
        if not known:
            return unknown_attempt(
                operation=OPERATION_REVIEW, transport=TRANSPORT_BATCH, batch_id="b", custom_id=cid, outcome=outcome
            )
        return known_attempt(
            {"input_tokens": 10, "output_tokens": 5}, operation=OPERATION_REVIEW, transport=TRANSPORT_BATCH,
            model=MODEL_OPUS_5, batch_id="b", custom_id=cid, outcome=outcome, output_channel=channel,
        )

    def test_rollup_counts_each_attempt_once(self):
        diag = DiagnosticsReport()
        attempts = [
            self._attempt("1", "ok", "tool"),
            self._attempt("2", "ok", "text"),
            self._attempt("3", "ok", "json"),
            self._attempt("4", "parse_error"),
            self._attempt("5", "incomplete"),
            self._attempt("6", "refusal"),
            self._attempt("7", "ok"),  # a record without a channel
            self._attempt("8", "pending", known=False),
        ]
        _review_event(diag, attempts)
        _review_event(diag, attempts[:2])  # the same attempts read twice
        rollup = diag.summary()["review_parse_outcomes"]
        assert rollup["attempts"] == 8
        assert rollup["by_outcome"] == {
            "ok": 4, "parse_error": 1, "incomplete": 1, "refusal": 1, "pending": 1,
        }
        assert rollup["by_output_channel"] == {"tool": 1, "text": 1, "json": 1, "unrecorded": 1}

    def test_measure_parse_outcomes(self):
        diag = DiagnosticsReport()
        _review_event(
            diag,
            [
                self._attempt("1", "ok", "tool"),
                self._attempt("2", "ok", "text"),
                self._attempt("3", "parse_error"),
                self._attempt("4", "incomplete"),
                self._attempt("5", "pending", known=False),
            ],
        )
        measured = eval_so.measure_parse_outcomes(json.loads(json.dumps(diag.summary(), default=str)))
        review = measured["review"]
        assert review["recorded"] is True
        assert review["attempts"] == 5
        assert review["responses"] == 4
        assert review["unparsed_rate"] == 0.5
        assert review["parse_error_rate"] == 0.25
        assert review["truncation_rate"] == 0.25
        assert review["text_fallback_rate"] == 0.5
        assert measured["verification"]["unreadable_rate"] is None

    def test_a_summary_without_the_rollup_is_not_read_as_zero_failures(self):
        measured = eval_so.measure_parse_outcomes({"verification_verdicts": {}})
        assert measured["review"]["recorded"] is False

    def test_verification_unreadable_verdicts(self):
        summary = {
            "review_parse_outcomes": {"attempts": 0, "by_outcome": {}, "by_output_channel": {}},
            "verification_verdicts": {"CONFIRMED": 6, "UNVERIFIED": 4},
            "retry_stats": {"by_terminal_reason": {"malformed_verdict": 1, "no_verdict": 1, "max_tokens": 1}},
        }
        verification = eval_so.measure_parse_outcomes(summary)["verification"]
        assert verification["unreadable_verdicts"] == {"no_verdict": 1, "malformed_verdict": 1}
        assert verification["unreadable_rate"] == 0.2


# ===========================================================================
# 6. The evaluation module
# ===========================================================================


class TestEvaluationModule:
    def test_inventory_covers_every_consumer_and_keeps_mechanisms_apart(self):
        rows = {row["consumer"]: row for row in eval_so.consumer_inventory()}
        assert set(rows) == {
            "review", "cross_check", "compliance", "drawing_impact", "research", "verification", "triage",
        }
        for row in rows.values():
            assert set(row["mechanisms"]) == set(eval_so.MECHANISMS)
            assert row["mechanisms"][eval_so.MECHANISM_STRICT]["state"] == eval_so.STATE_ON
        states = {c: {m: v["state"] for m, v in r["mechanisms"].items()} for c, r in rows.items()}
        # Forced tool use runs only on triage today.
        assert [c for c, s in states.items() if s[eval_so.MECHANISM_FORCED] == eval_so.STATE_ON] == ["triage"]
        # The review is the only experiment, and the first consumer.
        experiments = {c for c, s in states.items() if eval_so.STATE_EXPERIMENT in s.values()}
        assert experiments == {"review"} and rows["review"]["first_consumer"]
        # Every consumer with web tools is excluded from both new mechanisms.
        with_web = {c for c, r in rows.items() if r["shape"]["server_tools"]}
        assert with_web == {"research", "verification"}
        for consumer in with_web:
            assert states[consumer][eval_so.MECHANISM_FORCED] == eval_so.STATE_EXCLUDED
            assert states[consumer][eval_so.MECHANISM_FORMAT] == eval_so.STATE_EXCLUDED
            assert rows[consumer]["shape"]["citations_enabled"] is True

    def test_inventory_shapes_come_from_the_builders(self):
        rows = {row["consumer"]: row for row in eval_so.consumer_inventory()}
        assert rows["review"]["shape"]["tool_choice"] == ss.review_tool_choice()
        assert rows["cross_check"]["shape"]["tool_choice"] == ss.cross_check_tool_choice()
        assert rows["compliance"]["shape"]["tool_choice"] == ss.compliance_tool_choice()
        assert rows["triage"]["shape"]["tool_choice"] == ss.triage_tool_choice(model=api_config.TRIAGE_MODEL_DEFAULT)
        assert rows["verification"]["shape"]["tool_choice"] is None
        assert rows["research"]["shape"]["tool_choice"] is None
        assert rows["review"]["shape"]["model"] == api_config.REVIEW_MODEL_DEFAULT

    def test_review_arms(self):
        arms = eval_so.review_arm_requests()["arms"]
        assert arms["tool_auto"]["fields_changed"] == []
        assert arms["forced_tool"]["fields_changed"] == ["tool_choice"]
        assert arms["json_schema"]["fields_changed"] == [
            "messages", "output_config", "system", "tool_choice", "tools",
        ]
        assert arms["forced_tool"]["system_prompt_sha256"] == arms["tool_auto"]["system_prompt_sha256"]
        assert arms["json_schema"]["shape"]["output_format"]["type"] == "json_schema"

    def test_the_arm_switch_restores_the_environment(self, monkeypatch):
        monkeypatch.setenv(ENV, "forced_tool")
        with eval_so.review_output_constraint_switch("json_schema"):
            pass
        import os
        assert os.environ[ENV] == "forced_tool"

    def test_protocol_is_not_run(self):
        assert eval_so.EVALUATION_PROTOCOL["status"].startswith("NOT RUN")

    def test_cli(self, tmp_path, capsys):
        summary_path = tmp_path / "summary.json"
        summary_path.write_text(json.dumps({"review_parse_outcomes": {"attempts": 1, "by_outcome": {"ok": 1}, "by_output_channel": {"tool": 1}}}))
        assert eval_so.main(["--diagnostics", str(summary_path)]) == 0
        out = json.loads(capsys.readouterr().out)
        assert set(out) == {"inventory", "review_arms", "evaluation_protocol", "measured"}
        assert out["measured"]["review"]["text_fallback_rate"] == 0.0
