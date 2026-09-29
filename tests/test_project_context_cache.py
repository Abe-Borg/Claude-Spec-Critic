"""EX-01 (S20): the default-off Project Context breakpoint, and its layout tool.

The experiment was not evaluated live (plans/experiments/EX-01-project-context-
caching.md), so these tests make no claim about savings. They pin what the
switch does to a request and what it must never do:

* **Off by default, and off means byte-identical.** Unset, empty, a disable
  token, or an unrecognized value leaves every review request exactly as a
  build without the switch sends it.
* **On changes structure, never text.** The review user message becomes two
  text blocks that join to the same string; the head ends with the
  ``<project_context>`` block and carries the breakpoint.
* **The head is the shared prefix.** Every spec of one module and context,
  both transports, and the repair request carry the same head; a changed
  context or another module does not.
* **Within the provider's limits.** Three of four breakpoint slots, TTLs in
  the required order (longer first), no request-level field on a review.
* **Measurable per attempt.** A cache read or write on a review attempt is
  priced by TTL through the attempt records (plan WP-15).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals import project_context_cache as ex01
from src.core import api_config
from src.core.api_config import ENV_PROJECT_CONTEXT_CACHE, REVIEW_MODEL_DEFAULT
from src.core.code_cycles import CALIFORNIA_2025
from src.core.pricing import estimate_cost_breakdown
from src.core.request_budget import (
    count_request_digest,
    count_request_from_params,
    local_request_tokens,
)
from src.input.extractor import ExtractedSpec, extract_text_from_docx
from src.modules.registry import AVAILABLE_MODULES, get_module
from src.orchestration.diagnostics import DiagnosticsReport
from src.review import prompts
from src.review import realtime_review as rt
from src.review import review_request_builder as builder
from src.review.review_request_builder import (
    RETRY_TRUNCATED_REVIEW_INSTRUCTION,
    ReviewRequestSpec,
    build_review_request,
    build_user_content,
    build_user_message,
    build_user_message_parts,
)
from tests.fixtures import spec_docx as fx
from tests.fixtures.fake_anthropic import (
    FakeCacheCreation,
    FakeMessage,
    FakeToolUseBlock,
    FakeUsage,
    sample_review_findings_payload,
)

CONTEXT = (
    "Project: Example Elementary School modernization.\n"
    "Client: Example Unified School District.\n"
    "The mechanical systems serve classroom buildings A and B."
)
DC_MODULE = "datacenter_fire"


def _words(text: str) -> int:
    return len(text.split())


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No switch unless a test sets one; a word count for the local counter.

    The builder counts a request locally to decide the 300k-output path, and
    the cl100k rank file cannot be downloaded in a hermetic run.
    """
    monkeypatch.delenv(ENV_PROJECT_CONTEXT_CACHE, raising=False)
    monkeypatch.setattr(builder, "count_tokens", _words)


def _spec(filename: str, text: str | None = None) -> ExtractedSpec:
    body = text or f"SECTION {filename[:6]}\nPART 1 GENERAL\n1.01 SUMMARY\nA. Provide piping."
    return ExtractedSpec(filename=filename, content=body, word_count=len(body.split()))


def _request_spec(spec: ExtractedSpec, **overrides) -> ReviewRequestSpec:
    fields = dict(
        spec_content=spec.content,
        filename=spec.filename,
        model=REVIEW_MODEL_DEFAULT,
        cycle=CALIFORNIA_2025,
        project_context=CONTEXT,
        paragraph_map=spec.paragraph_map,
    )
    fields.update(overrides)
    return ReviewRequestSpec(**fields)


def _content(params: dict):
    (message,) = params["messages"]
    return message["content"]


def _joined(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(block["text"] for block in content)


@pytest.fixture
def docx_specs(tmp_path) -> list[ExtractedSpec]:
    """Three real extractions (paragraph maps, element ids) from the shared fixtures."""
    paths = [
        fx.save_docx(fx.build_clean_three_part(), tmp_path, "230500.docx"),
        fx.save_docx(fx.build_table_only_article(), tmp_path, "230593.docx"),
        fx.save_docx(fx.build_auto_numbered_three_part(), tmp_path, "232113.docx"),
    ]
    return [extract_text_from_docx(p) for p in paths]


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


class TestSwitchValues:
    @pytest.mark.parametrize("value", [None, "", "  ", "0", "false", "No", "OFF"])
    def test_off(self, monkeypatch, value):
        if value is not None:
            monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, value)
        assert api_config.project_context_cache_control() is None

    @pytest.mark.parametrize("value", ["1h", "1H", " 1h ", "1", "true", "yes", "on"])
    def test_one_hour(self, monkeypatch, value):
        monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, value)
        assert api_config.project_context_cache_control() == {"type": "ephemeral", "ttl": "1h"}

    def test_five_minutes(self, monkeypatch):
        monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, "5m")
        assert api_config.project_context_cache_control() == {"type": "ephemeral", "ttl": "5m"}

    def test_an_unrecognized_value_fails_closed_and_warns_once(self, monkeypatch, caplog):
        monkeypatch.setattr(api_config, "_WARNED_PROJECT_CONTEXT_CACHE_VALUES", set())
        monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, "5min")
        with caplog.at_level(logging.WARNING, logger=api_config.__name__):
            assert api_config.project_context_cache_control() is None
            assert api_config.project_context_cache_control() is None
        warnings = [r for r in caplog.records if ENV_PROJECT_CONTEXT_CACHE in r.getMessage()]
        assert len(warnings) == 1
        assert "stays off" in warnings[0].getMessage()

    def test_each_call_returns_its_own_dict(self, monkeypatch):
        """A caller mutating one request's marker cannot change the next one's."""
        monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, "5m")
        first = api_config.project_context_cache_control()
        first["ttl"] = "1h"
        assert api_config.project_context_cache_control() == {"type": "ephemeral", "ttl": "5m"}


# ---------------------------------------------------------------------------
# Head and tail
# ---------------------------------------------------------------------------


class TestHeadAndTail:
    @pytest.mark.parametrize("module_id", ["california_k12_mep", DC_MODULE])
    @pytest.mark.parametrize("context", ["", "   ", CONTEXT])
    @pytest.mark.parametrize("with_alerts", [False, True])
    def test_head_plus_tail_is_the_message(self, docx_specs, module_id, context, with_alerts):
        spec = docx_specs[0]
        cycle = get_module(module_id).cycle
        alerts = (
            [
                {
                    "filename": spec.filename,
                    "type": "Placeholder",
                    "match": "[TBD]",
                    "context": "Provide [TBD].",
                    "position": 0,
                    "deterministic_rule": "placeholder",
                }
            ]
            if with_alerts
            else None
        )
        kwargs = dict(cycle=cycle, paragraph_map=spec.paragraph_map, pre_detected_alerts=alerts)
        head, tail = prompts.get_single_spec_user_message_parts(
            spec.content, spec.filename, context, **kwargs
        )
        whole = prompts.get_single_spec_user_message(spec.content, spec.filename, context, **kwargs)
        assert head + tail == whole
        assert tail.startswith("<spec ")
        assert spec.filename not in head
        if context.strip():
            assert head.endswith("</project_context>\n\n")
            assert context.strip() in head
        else:
            assert "<project_context>" not in head

    def test_everything_per_spec_is_in_the_tail(self, docx_specs):
        spec = docx_specs[0]
        alerts = [
            {
                "filename": spec.filename,
                "type": "Placeholder",
                "match": "[TBD]",
                "context": "Provide [TBD].",
                "position": 0,
                "deterministic_rule": "placeholder",
            }
        ]
        head, tail = build_user_message_parts(
            _request_spec(
                spec,
                pre_detected_alerts=alerts,
                retry_instruction=RETRY_TRUNCATED_REVIEW_INSTRUCTION,
            )
        )
        for per_spec in ("<pre_detected", RETRY_TRUNCATED_REVIEW_INSTRUCTION, "<final_task>"):
            assert per_spec in tail and per_spec not in head
        assert tail.endswith(RETRY_TRUNCATED_REVIEW_INSTRUCTION)


# ---------------------------------------------------------------------------
# Off: byte-identical
# ---------------------------------------------------------------------------


class TestOffIsByteIdentical:
    @pytest.mark.parametrize("value", [None, "0", "off", "bogus"])
    def test_the_request_does_not_change(self, monkeypatch, docx_specs, value):
        request_spec = _request_spec(docx_specs[0])
        reference = json.dumps(build_review_request(request_spec).params, sort_keys=True)
        if value is not None:
            monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, value)
        built = build_review_request(request_spec)
        assert json.dumps(built.params, sort_keys=True) == reference
        assert _content(built.params) == build_user_message(request_spec)
        assert isinstance(_content(built.params), str)

    def test_no_request_level_cache_field_on_a_review(self, docx_specs):
        assert "cache_control" not in build_review_request(_request_spec(docx_specs[0])).params


# ---------------------------------------------------------------------------
# On: two blocks, same text
# ---------------------------------------------------------------------------


class TestOnChangesStructureOnly:
    @pytest.mark.parametrize("ttl", ["1h", "5m"])
    def test_two_blocks_that_join_to_the_same_text(self, monkeypatch, docx_specs, ttl):
        request_spec = _request_spec(docx_specs[0])
        baseline = build_review_request(request_spec).params
        monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, ttl)
        built = build_review_request(request_spec)
        content = _content(built.params)

        head, tail = build_user_message_parts(request_spec)
        assert content == [
            {"type": "text", "text": head, "cache_control": {"type": "ephemeral", "ttl": ttl}},
            {"type": "text", "text": tail},
        ]
        assert _joined(content) == _content(baseline) == built.user_message
        # Nothing else in the request moved.
        assert {k: v for k, v in built.params.items() if k != "messages"} == {
            k: v for k, v in baseline.items() if k != "messages"
        }

    @pytest.mark.parametrize("context", ["", " \n "])
    def test_without_a_context_the_message_stays_one_string(self, monkeypatch, docx_specs, context):
        request_spec = _request_spec(docx_specs[0], project_context=context)
        baseline = json.dumps(build_review_request(request_spec).params, sort_keys=True)
        monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, "1h")
        assert json.dumps(build_review_request(request_spec).params, sort_keys=True) == baseline
        assert build_user_content(request_spec) == build_user_message(request_spec)

    @pytest.mark.parametrize("ttl", ["1h", "5m"])
    def test_within_the_breakpoint_limit_and_ttl_order(self, monkeypatch, docx_specs, ttl):
        monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, ttl)
        params = build_review_request(_request_spec(docx_specs[0])).params
        points = ex01.breakpoints(params)
        assert [(p.position, p.ttl) for p in points] == [
            ("tools[0]", "1h"),
            ("system[0]", "1h"),
            ("messages[0].content[0]", ttl),
        ]
        assert len(points) <= ex01.MAX_BREAKPOINTS
        assert ex01.ttl_order_problems(points) == []
        assert "cache_control" not in params  # a review never resumes


# ---------------------------------------------------------------------------
# The head is the shared prefix
# ---------------------------------------------------------------------------


class TestTheHeadIsTheSharedPrefix:
    def test_every_spec_of_a_module_shares_the_head(self, monkeypatch, docx_specs):
        monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, "1h")
        heads = {
            json.dumps(_content(build_review_request(_request_spec(s)).params)[0], sort_keys=True)
            for s in docx_specs
        }
        tails = {
            _content(build_review_request(_request_spec(s)).params)[1]["text"] for s in docx_specs
        }
        assert len(heads) == 1
        assert len(tails) == len(docx_specs)

    def test_the_baseline_already_shares_exactly_that_prefix(self, docx_specs):
        """The candidate adds a breakpoint where the requests already part."""
        baseline = [build_review_request(_request_spec(s)).params for s in docx_specs]
        shared = ex01.shared_prefix(baseline)
        head, _tail = build_user_message_parts(_request_spec(docx_specs[0]))
        assert shared["settings_ahead_of_messages_agree"] is True
        assert shared["identical_segments"] == ["tools[0]", "system[0]"]
        assert shared["divergence"]["position"] == "messages[0].content[0]"
        # The messages part at the spec: the common text is the head plus the
        # part of the opening ``<spec filename="`` tag the file names share.
        common = shared["divergence"]["common_chars_in_segment"]
        assert common >= len(head)
        tails = [_content(p)[len(head):] for p in baseline]
        assert all(t.startswith("<spec ") for t in tails)

    def test_the_candidate_moves_the_divergence_past_the_breakpoint(self, monkeypatch, docx_specs):
        monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, "1h")
        candidate = [build_review_request(_request_spec(s)).params for s in docx_specs]
        shared = ex01.shared_prefix(candidate)
        assert shared["identical_segments"] == ["tools[0]", "system[0]", "messages[0].content[0]"]
        assert shared["divergence"]["position"] == "messages[0].content[1]"

    def test_a_changed_context_invalidates_the_head(self, monkeypatch, docx_specs):
        """The changed-context control: one character, a different head."""
        monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, "1h")
        spec = docx_specs[0]
        before = build_review_request(_request_spec(spec)).params
        after = build_review_request(_request_spec(spec, project_context=CONTEXT + ".")).params
        shared = ex01.shared_prefix([before, after])
        assert shared["identical_segments"] == ["tools[0]", "system[0]"]
        assert shared["divergence"]["position"] == "messages[0].content[0]"

    def test_another_module_has_its_own_prefix(self, monkeypatch, docx_specs):
        monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, "1h")
        spec = docx_specs[0]
        ca = build_review_request(_request_spec(spec)).params
        dc = build_review_request(
            _request_spec(spec, cycle=AVAILABLE_MODULES[DC_MODULE].cycle)
        ).params
        assert ca["system"] != dc["system"]
        assert _content(ca)[0]["text"] != _content(dc)[0]["text"]

    def test_the_repair_request_reads_the_primary_head(self, monkeypatch, docx_specs):
        monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, "1h")
        spec = docx_specs[0]
        primary = build_review_request(_request_spec(spec)).params
        repair = build_review_request(
            _request_spec(spec, retry_instruction=RETRY_TRUNCATED_REVIEW_INSTRUCTION)
        ).params
        assert _content(repair)[0] == _content(primary)[0]
        assert _content(repair)[1]["text"] == (
            _content(primary)[1]["text"] + "\n\n" + RETRY_TRUNCATED_REVIEW_INSTRUCTION
        )
        assert {k: v for k, v in repair.items() if k != "messages"} == {
            k: v for k, v in primary.items() if k != "messages"
        }


# ---------------------------------------------------------------------------
# Both transports send it
# ---------------------------------------------------------------------------


class TestBothTransports:
    def test_every_batch_item_carries_the_identical_marked_head(self, monkeypatch, docx_specs):
        """The provider's batch guidance: identical cache_control blocks in every item."""
        from src.batch import batch as batch_mod

        monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, "1h")
        submitted: list[list[dict]] = []

        def fake_create(client, batch_requests, *, use_beta, model):
            submitted.append(batch_requests)
            return SimpleNamespace(id="msgbatch_1"), False

        monkeypatch.setattr(batch_mod, "_create_review_batch", fake_create)
        monkeypatch.setattr(batch_mod, "_get_client", lambda **_: SimpleNamespace())
        batch_mod.submit_review_batch(docx_specs, project_context=CONTEXT, cycle=CALIFORNIA_2025)

        (requests,) = submitted
        heads = [_content(r["params"])[0] for r in requests]
        assert len(requests) == 3
        assert all(h == heads[0] for h in heads)
        assert heads[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        for request, spec in zip(requests, docx_specs):
            assert spec.filename in _content(request["params"])[1]["text"]

    def test_the_realtime_stream_and_its_inline_repair(self, monkeypatch):
        """Real time sends the same blocks, and the repair keeps the head."""
        from tests.fixtures.fake_anthropic import max_tokens_incomplete_response

        monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, "1h")
        monkeypatch.setattr(rt, "review_extended_output_count", lambda request_spec: 10)
        monkeypatch.setattr(rt.time, "sleep", lambda _s: None)
        calls: list[dict] = []
        script = iter([max_tokens_incomplete_response(), _review_message()])

        def stream(**kwargs):
            calls.append(kwargs)
            return _StreamOf(next(script))

        client = SimpleNamespace(messages=SimpleNamespace(stream=stream))
        monkeypatch.setattr(rt, "_get_client", lambda **_: client)

        spec = _spec("230500.docx")
        results, _map = rt.run_realtime_review([spec], project_context=CONTEXT)

        assert results["review__230500__0"].parse_status == "ok"
        primary, repair = calls
        batch_shape = build_review_request(
            _request_spec(spec, paragraph_map=None, force_allow_extended_output=False)
        ).params
        assert primary["messages"] == batch_shape["messages"]
        assert _content(repair)[0] == _content(primary)[0]
        assert _content(repair)[1]["text"].endswith(RETRY_TRUNCATED_REVIEW_INSTRUCTION)


# ---------------------------------------------------------------------------
# Sizing and pricing
# ---------------------------------------------------------------------------


class TestSizingAndPricing:
    def test_the_counting_form_counts_the_same_text(self, monkeypatch, docx_specs):
        request_spec = _request_spec(docx_specs[0])
        baseline = count_request_from_params(build_review_request(request_spec).params)
        monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, "1h")
        candidate = count_request_from_params(build_review_request(request_spec).params)

        (message,) = candidate["messages"]
        assert all("cache_control" not in block for block in message["content"])
        assert _joined(message["content"]) == baseline["messages"][0]["content"]
        # Split at a whitespace boundary: a word count sees the same request.
        assert local_request_tokens(candidate, counter=_words) == local_request_tokens(
            baseline, counter=_words
        )
        # A different shape is a different count-cache key, so an estimate
        # for one shape is never reused for the other.
        assert count_request_digest(candidate) != count_request_digest(baseline)

    def test_review_sizing_reads_the_shape_it_sends(self, monkeypatch, docx_specs):
        """The preflight's estimate for the two-block shape is the one read back.

        The count cache is keyed by the exact counting form, so sizing must
        build the same two-block shape the request sends; sizing the one-string
        shape would read (or miss) another request's estimate.
        """
        from src.core import request_budget

        request_spec = _request_spec(docx_specs[0])
        monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, "1h")
        sent = count_request_from_params(build_review_request(request_spec).params)
        request_budget._COUNT_CACHE.put(count_request_digest(sent), 123_456)

        count = builder.review_input_count(request_spec)

        assert count.tokens == 123_456
        assert count.source == "api_estimate"

    def test_a_review_attempt_prices_its_cache_write_and_read_by_ttl(self, monkeypatch):
        """Item 6 of EX-01: reads and writes are visible, per attempt, per TTL."""
        monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, "1h")
        monkeypatch.setattr(rt, "review_extended_output_count", lambda request_spec: 10)
        write = _review_message(
            usage=FakeUsage(
                input_tokens=300,
                output_tokens=500,
                cache_creation_input_tokens=4_000,
                cache_creation=FakeCacheCreation(ephemeral_1h_input_tokens=4_000),
            )
        )
        read = _review_message(
            usage=FakeUsage(input_tokens=300, output_tokens=500, cache_read_input_tokens=4_000)
        )
        script = iter([write, read])
        client = SimpleNamespace(
            messages=SimpleNamespace(stream=lambda **_kw: _StreamOf(next(script)))
        )
        monkeypatch.setattr(rt, "_get_client", lambda **_: client)
        diag = DiagnosticsReport()

        rt.run_realtime_review(
            [_spec("230500.docx"), _spec("230593.docx")],
            project_context=CONTEXT,
            model=REVIEW_MODEL_DEFAULT,
            max_workers=1,
            diagnostics=diag,
        )

        summary = diag.summary()["cost_summary"]
        assert summary["total_cache_creation_input_tokens"] == 4_000
        assert summary["total_cache_read_input_tokens"] == 4_000
        assert summary["cache_write_breakdown"]["1h_tokens"] == 4_000
        assert summary["cache_write_breakdown"]["unknown_tokens"] == 0
        expected = estimate_cost_breakdown(
            300, 500, model=REVIEW_MODEL_DEFAULT,
            cache_creation_input_tokens=4_000,
            cache_creation_1h_input_tokens=4_000,
            cache_creation_unknown_input_tokens=0,
        ).total + estimate_cost_breakdown(
            300, 500, model=REVIEW_MODEL_DEFAULT, cache_read_input_tokens=4_000
        ).total
        assert summary["estimated_cost_usd"]["total"] == pytest.approx(expected, abs=1e-6)


class _StreamOf:
    """A finished stream: no text chunks, then the final message."""

    text_stream = ()

    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


def _review_message(*, usage: FakeUsage | None = None) -> FakeMessage:
    return FakeMessage(
        content=[
            FakeToolUseBlock(name="submit_review_findings", input=sample_review_findings_payload())
        ],
        stop_reason="tool_use",
        usage=usage or FakeUsage(),
        model=REVIEW_MODEL_DEFAULT,
    )


# ---------------------------------------------------------------------------
# The layout tool
# ---------------------------------------------------------------------------


class TestLayoutTool:
    def test_breakpoints_in_prefix_order_with_the_automatic_one_last(self):
        params = {
            "tools": [{"name": "a"}, {"name": "b", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            "system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "u", "cache_control": {"type": "ephemeral"}},
                ]},
            ],
            "cache_control": {"type": "ephemeral"},
        }
        points = ex01.breakpoints(params)
        assert [(p.position, p.ttl, p.automatic) for p in points] == [
            ("tools[1]", "1h", False),
            ("system[0]", "1h", False),
            ("messages[0].content[0]", "5m", False),
            ("request", "5m", True),
        ]
        assert ex01.ttl_order_problems(points) == []

    def test_a_long_ttl_after_a_short_one_is_a_problem(self):
        params = {
            "system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral", "ttl": "5m"}}],
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "u", "cache_control": {"type": "ephemeral", "ttl": "1h"}},
            ]}],
        }
        problems = ex01.ttl_order_problems(ex01.breakpoints(params))
        assert problems == ["messages[0].content[0] (1h) follows system[0] (5m)"]

    def test_shared_prefix_notices_a_setting_that_invalidates_messages(self, docx_specs):
        first = build_review_request(_request_spec(docx_specs[0])).params
        second = dict(build_review_request(_request_spec(docx_specs[1])).params)
        second["tool_choice"] = {"type": "any"}
        assert ex01.shared_prefix([first, second])["settings_ahead_of_messages_agree"] is False

    def test_the_budget_of_every_phase(self):
        baseline = ex01.phase_breakpoint_budget(None)
        candidate = ex01.phase_breakpoint_budget("1h")
        review_rows = {"review (batch)", "review (real time)", "review repair"}
        assert set(baseline) == set(candidate)
        for label, row in candidate.items():
            assert row["breakpoint_count"] <= ex01.MAX_BREAKPOINTS, label
            assert not row.get("ttl_order_problems"), label
            expected = baseline[label]["breakpoint_count"] + (1 if label in review_rows else 0)
            assert row["breakpoint_count"] == expected, label
        # The resume loops' request-level breakpoint is counted.
        assert baseline["verification (real-time pause_turn resume)"]["breakpoint_count"] == 3
        assert baseline["requirements research (pause_turn resume)"]["breakpoint_count"] == 3
        assert baseline["verification triage"]["breakpoint_count"] == 0
        # Cross-check and compliance carry the context but are left alone.
        assert candidate["cross-check"]["carries_project_context"] is True
        assert candidate["cross-check"]["breakpoint_count"] == 2
        assert candidate["compliance"]["breakpoint_count"] == 2

    def test_the_switch_block_restores_the_environment(self, monkeypatch):
        monkeypatch.setenv(ENV_PROJECT_CONTEXT_CACHE, "5m")
        with ex01.project_context_cache_switch("1h"):
            assert api_config.project_context_cache_control()["ttl"] == "1h"
        assert api_config.project_context_cache_control()["ttl"] == "5m"
        with ex01.project_context_cache_switch(None):
            assert api_config.project_context_cache_control() is None
        assert api_config.project_context_cache_control()["ttl"] == "5m"

    def test_break_even_write_share(self):
        assert ex01.break_even_write_share("1h") == pytest.approx(0.9 / 1.9)
        assert ex01.break_even_write_share("5m") == pytest.approx(0.9 / 1.15)

    def test_the_protocol_says_not_run(self):
        assert ex01.EVALUATION_PROTOCOL["status"].startswith("NOT RUN")
        for key in ("arms", "cold_and_warm", "changed_context_control", "promotion"):
            assert ex01.EVALUATION_PROTOCOL[key]

    def test_an_unknown_module_is_an_error_not_california(self, docx_specs):
        with pytest.raises(Exception):
            ex01.review_requests(docx_specs[:1], project_context=CONTEXT, module_id="no_such_module")

    def test_the_command_line_capture(self, tmp_path, capsys):
        paths = [
            fx.save_docx(fx.build_clean_three_part(), tmp_path, "230500.docx"),
            fx.save_docx(fx.build_table_only_article(), tmp_path, "230593.docx"),
        ]
        context_file = tmp_path / "context.txt"
        context_file.write_text(CONTEXT, encoding="utf-8")
        argv = [a for p in paths for a in ("--spec", str(p))] + ["--context-file", str(context_file)]

        assert ex01.main(argv) == 0

        result = json.loads(capsys.readouterr().out)
        assert result["baseline"]["shared_prefix"]["identical_segments"] == ["tools[0]", "system[0]"]
        assert result["candidate"]["shared_prefix"]["identical_segments"][-1] == "messages[0].content[0]"
        assert result["changed_context_control"]["divergence"]["position"] == "messages[0].content[0]"
        assert [s["file"] for s in result["inputs"]["specs"]] == ["230500.docx", "230593.docx"]
        # The dataset is identified by what the requests carry, not by zip bytes.
        import hashlib

        expected = [
            hashlib.sha256(extract_text_from_docx(p).content.encode("utf-8")).hexdigest()
            for p in paths
        ]
        assert [s["content_sha256"] for s in result["inputs"]["specs"]] == expected
        assert len(result["configuration_sha256"]) == 64
        for request in result["candidate"]["requests"]:
            assert request["breakpoint_count"] == 3
            assert request["within_breakpoint_limit"] is True
        # The capture leaves the switch as it found it (unset).
        assert api_config.project_context_cache_control() is None
