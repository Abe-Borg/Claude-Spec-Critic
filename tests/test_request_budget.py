"""Model-aware request budgets (plan WP-08, chunk S06).

Two halves:

* **The contract** (``src/core/request_budget.py``): the counting form is the
  sent request's own fields; a count-API answer is an *estimate* and a
  malformed one is never a number; the local fallback pads every counted
  part, tool overhead included; the ceiling is the smaller of the phase
  limit and the model's window minus the real output cap and a reserve;
  estimates are cached per model and exact shape only.
* **The nine WP-08 acceptance cases**, driven through the real cross-check,
  compliance, review-builder, and real-time code with stubbed counts — never
  a giant synthetic request and never the network (the live endpoint is
  disabled in ``tests/conftest.py``).
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

import src.compliance.compliance_checker as compliance
import src.cross_check.cross_checker as cc
import src.review.review_request_builder as builder
from src.core import request_budget as rb
from src.core import tokenizer as tk
from src.core.api_config import (
    CROSS_CHECK_MODEL_DEFAULT,
    LARGE_REVIEW_INPUT_THRESHOLD,
    MODEL_HAIKU_45,
    MODEL_OPUS_5,
    MODEL_SONNET_46,
    MODEL_SONNET_5,
    REVIEW_OUTPUT_CAP,
    REVIEW_OUTPUT_CAP_BATCH_EXTENDED,
    cross_check_max_tokens,
)
from src.core.chunked_pass import plan_chunks
from src.core.code_cycles import DEFAULT_CYCLE
from src.core.tokenizer import CROSS_CHECK_RECOMMENDED_MAX
from src.input.extractor import ExtractedSpec
from src.modules import DEFAULT_MODULE
from src.research import DimensionStatus, RequirementsProfile, ResearchItem
from src.review import realtime_review as rt
from src.review.review_request_builder import (
    ReviewRequestSpec,
    build_review_request,
    review_request_budget,
)
from src.review.structured_schemas import COMPLIANCE_TOOL_NAME, CROSS_CHECK_TOOL_NAME
from tests.fixtures.count_api import CountingClient, count_response, user_text
from tests.fixtures.fake_anthropic import FakeMessage, FakeToolUseBlock

_GROUPS = DEFAULT_MODULE.cross_check_chunk_groups
_SPEC_NAME = re.compile(r'<spec filename="([^"]+)"')


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


def _spec(filename: str, content: str = "Provide equipment per code.") -> ExtractedSpec:
    return ExtractedSpec(filename=filename, content=content, word_count=len(content.split()))


def sized(sizes: dict[str, int], *, base: int = 10_000, marker: str = "", per_marker: int = 0):
    """A count function: ``base`` + the size of every spec in the request.

    ``marker``/``per_marker`` add ``per_marker`` tokens per occurrence of
    ``marker`` anywhere in the user message (project-context overhead).
    """

    def count(request: dict) -> int:
        text = user_text(request)
        total = base + sum(sizes.get(name, 0) for name in _SPEC_NAME.findall(text))
        if marker:
            total += per_marker * text.count(marker)
        return total

    return count


class _Stream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @property
    def text_stream(self):
        return iter(())

    def get_final_message(self):
        return self._message


def _cross_check_ok(findings=None) -> FakeMessage:
    return FakeMessage(
        content=[
            FakeToolUseBlock(
                name=CROSS_CHECK_TOOL_NAME,
                input={
                    "findings": list(findings or []),
                    "coordination_summary": "Coordination appears adequate.",
                },
            )
        ],
        stop_reason="tool_use",
    )


def _compliance_ok() -> FakeMessage:
    return FakeMessage(
        content=[
            FakeToolUseBlock(
                name=COMPLIANCE_TOOL_NAME,
                input={"compliance_summary": "Fine.", "coverage": [], "findings": []},
            )
        ],
        stop_reason="tool_use",
    )


class _Messages:
    def __init__(self, owner: "PassClient"):
        self._owner = owner

    def stream(self, **kwargs):
        self._owner.stream_calls.append(kwargs)
        result = self._owner.respond(kwargs)
        if isinstance(result, Exception):
            raise result
        return _Stream(result)

    def count_tokens(self, **kwargs):
        self._owner.count_calls.append(kwargs)
        if self._owner.count_fn is None:
            raise RuntimeError("count API unavailable")
        return count_response(self._owner.count_fn(kwargs))


class PassClient:
    """Streams scripted pass responses; answers ``count_tokens`` with ``count_fn``.

    ``count_fn=None`` models an unavailable count API (every call raises).
    Deliberately has no ``beta`` attribute: a path reaching for the batch-only
    surface fails loudly.
    """

    def __init__(self, *, count_fn=None, respond=None):
        self.count_fn = count_fn
        self.respond = respond or (lambda _kwargs: _cross_check_ok())
        self.stream_calls: list[dict] = []
        self.count_calls: list[dict] = []
        self.messages = _Messages(self)

    def streamed_specs(self) -> list[list[str]]:
        return [_SPEC_NAME.findall(user_text(call)) for call in self.stream_calls]


def _install(monkeypatch, module, client) -> None:
    monkeypatch.setattr(module, "_get_client", lambda *_a, **_k: client)


def _profile() -> RequirementsProfile:
    return RequirementsProfile(
        items=[
            ResearchItem(
                item_id="r-aaaaaaaaaaaa",
                dimension_id="governing_codes",
                topic="Topic",
                category="governing_code",
                requirement="The adopted edition governs.",
                grounded=True,
                accepted_sources=["https://codes.example.gov/x"],
                confidence=0.8,
            )
        ],
        dimension_statuses=[DimensionStatus(dimension_id="governing_codes", status="completed")],
        research_date="2026-07-14",
        project={"city": "Markham", "state_or_province": "ON", "country": "CA", "client_name": "X"},
    )


_FOUR = [
    "22 11 00 Water.docx",
    "22 13 00 Sanitary.docx",
    "23 05 00 HVAC.docx",
    "23 07 00 Insulation.docx",
]


def _four_specs() -> list[ExtractedSpec]:
    return [_spec(name, f"Section body for {name}.") for name in _FOUR]


def _count_of(client: PassClient, count_fn, call: dict) -> int:
    """The scripted estimate of a request that was actually streamed."""
    return count_fn(rb.count_request_from_params(call))


# ===========================================================================
# 1. The contract
# ===========================================================================


class TestCountValidation:
    """Count-API answers are estimates; malformed ones are never a number."""

    @pytest.mark.parametrize(
        "response",
        [
            SimpleNamespace(input_tokens=0),
            SimpleNamespace(input_tokens=-5),
            SimpleNamespace(input_tokens=None),
            SimpleNamespace(input_tokens=True),
            SimpleNamespace(input_tokens="900000"),
            SimpleNamespace(input_tokens=1.5),
            SimpleNamespace(),
            {"input_tokens": 0},
            {},
        ],
        ids=["zero", "negative", "none", "bool", "string", "float", "missing", "dict-zero", "dict-missing"],
    )
    def test_malformed_responses_never_become_a_count(self, response):
        result = tk.validated_input_tokens(response)
        assert result.tokens is None
        assert result.error

    def test_a_positive_integer_is_accepted(self):
        assert tk.validated_input_tokens(SimpleNamespace(input_tokens=42)).tokens == 42
        assert tk.validated_input_tokens({"input_tokens": 7}).tokens == 7

    def test_count_tokens_via_api_returns_none_not_zero(self):
        client = CountingClient(lambda _request: 0)
        assert tk.count_tokens_via_api(
            model=MODEL_SONNET_5, system="s", messages=[{"role": "user", "content": "x"}],
            client=client,
        ) is None

    def test_a_failed_call_is_reported_not_raised(self):
        client = PassClient(count_fn=None)
        result = tk.count_input_tokens(
            model=MODEL_SONNET_5, messages=[{"role": "user", "content": "x"}], client=client
        )
        assert result.tokens is None
        assert "count API call failed" in result.error

    def test_a_client_that_cannot_count_never_takes_a_permit(self):
        class Gate:
            entered = 0

            def __enter__(self):
                Gate.entered += 1

            def __exit__(self, *_a):
                return False

        result = tk.count_input_tokens(
            model=MODEL_SONNET_5,
            messages=[{"role": "user", "content": "x"}],
            client=SimpleNamespace(messages=SimpleNamespace()),
            call_gate=Gate(),
        )
        assert result.tokens is None
        assert Gate.entered == 0


def _params(**overrides) -> dict:
    params = {
        "model": MODEL_SONNET_5,
        "max_tokens": 96_000,
        "system": [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        "messages": [{"role": "user", "content": "USER"}],
        "tools": [{"name": "t", "input_schema": {"type": "object"}, "cache_control": {"type": "ephemeral"}}],
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
        "service_tier": "auto",
    }
    params.update(overrides)
    return params


class TestCountingForm:
    def test_keeps_exactly_the_counted_fields_without_cache_control(self):
        form = rb.count_request_from_params(_params(container="c-1"))
        assert set(form) == {"model", "system", "messages", "tools", "tool_choice", "thinking"}
        assert form["system"] == [{"type": "text", "text": "SYSTEM"}]
        assert form["tools"] == [{"name": "t", "input_schema": {"type": "object"}}]

    def test_is_a_copy(self):
        params = _params()
        form = rb.count_request_from_params(params)
        form["messages"][0]["content"] = "changed"
        assert params["messages"][0]["content"] == "USER"

    @pytest.mark.parametrize(
        "change",
        [
            {"model": MODEL_HAIKU_45},
            {"system": "OTHER"},
            {"messages": [{"role": "user", "content": "OTHER"}]},
            {"tools": [{"name": "u", "input_schema": {"type": "object"}}]},
            {"tool_choice": {"type": "any"}},
            {"thinking": None},
        ],
        ids=["model", "system", "messages", "tools", "tool_choice", "thinking"],
    )
    def test_every_shape_input_changes_the_digest(self, change):
        base = rb.count_request_digest(rb.count_request_from_params(_params()))
        other = rb.count_request_digest(rb.count_request_from_params(_params(**change)))
        assert base != other

    @pytest.mark.parametrize(
        "change",
        [{"max_tokens": 1}, {"output_config": {"effort": "low"}}, {"service_tier": None},
         {"system": [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral", "ttl": "5m"}}]}],
        ids=["max_tokens", "effort", "service_tier", "cache-ttl"],
    )
    def test_output_and_pricing_settings_do_not(self, change):
        base = rb.count_request_digest(rb.count_request_from_params(_params()))
        other = rb.count_request_digest(rb.count_request_from_params(_params(**change)))
        assert base == other


class TestCountCache:
    def test_an_identical_shape_is_counted_once(self):
        client = CountingClient(lambda _r: 1_000)
        first = rb.request_budget(_params(), client_factory=lambda: client)
        second = rb.request_budget(_params(), client_factory=lambda: client)
        assert first.count == second.count == 1_000
        assert len(client.count_calls) == 1

    def test_never_reused_across_a_model_override(self):
        client = CountingClient(lambda request: 1_000 if request["model"] == MODEL_SONNET_5 else 2_000)
        sonnet = rb.request_budget(_params(), client_factory=lambda: client)
        haiku = rb.request_budget(_params(model=MODEL_HAIKU_45), client_factory=lambda: client)
        assert (sonnet.count, haiku.count) == (1_000, 2_000)
        assert [call["model"] for call in client.count_calls] == [MODEL_SONNET_5, MODEL_HAIKU_45]

    def test_invalid_answers_are_not_cached(self):
        answers = iter([0, 5_000])
        client = CountingClient(lambda _r: next(answers))
        first = rb.request_budget(_params(), client_factory=lambda: client, local_counter=len)
        second = rb.request_budget(_params(), client_factory=lambda: client, local_counter=len)
        assert first.count_source == rb.COUNT_SOURCE_LOCAL_PADDED
        assert second.count_source == rb.COUNT_SOURCE_API and second.count == 5_000

    def test_use_api_false_reads_the_cache_without_calling(self):
        client = CountingClient(lambda _r: 3_000)
        rb.request_budget(_params(), client_factory=lambda: client)
        cached = rb.request_budget(_params(), use_api=False, local_counter=lambda _t: 1)
        assert cached.count_source == rb.COUNT_SOURCE_API and cached.count == 3_000
        assert len(client.count_calls) == 1

    def test_use_api_false_never_calls_even_with_an_empty_cache(self):
        client = CountingClient(lambda _r: 3_000)
        budget = rb.request_budget(
            _params(), use_api=False, client_factory=lambda: client, local_counter=lambda _t: 1
        )
        assert budget.count_source == rb.COUNT_SOURCE_LOCAL_PADDED
        assert client.count_calls == []

    def test_a_disabled_preflight_never_calls(self, monkeypatch):
        monkeypatch.setattr("src.core.api_config.token_count_preflight_enabled", lambda: False)
        client = CountingClient(lambda _r: 3_000)
        budget = rb.request_budget(_params(), client_factory=lambda: client, local_counter=lambda _t: 1)
        assert budget.count_source == rb.COUNT_SOURCE_LOCAL_PADDED
        assert "disabled" in budget.unavailable_reason
        assert client.count_calls == []


class TestLocalPadding:
    def test_every_counted_part_is_padded_tool_overhead_included(self):
        form = rb.count_request_from_params(_params())
        words = lambda text: len(text.split())  # noqa: E731
        local = rb.local_request_tokens(form, counter=words)
        import json

        expected = (
            words("SYSTEM") + words("USER")
            + words(json.dumps(form["tools"][0], sort_keys=True, ensure_ascii=False))
            + words(json.dumps(form["tool_choice"], sort_keys=True))
            + rb.TOOL_USE_SYSTEM_PROMPT_ALLOWANCE
        )
        assert local == expected
        budget = rb.request_budget(_params(), use_api=False, local_counter=words)
        factor = tk.local_estimate_safety_factor(MODEL_SONNET_5)
        assert budget.count == tk.safe_local_estimate(expected, model=MODEL_SONNET_5)
        assert budget.count > expected
        assert budget.local_tokens == expected and budget.padding_factor == factor

    def test_no_tools_means_no_tool_allowance(self):
        form = rb.count_request_from_params(_params(tools=None, tool_choice=None))
        assert rb.local_request_tokens(form, counter=lambda _t: 1) == 2  # system + message

    def test_the_allowance_covers_every_registered_models_documented_overhead(self):
        # Anthropic's tool-use table: the largest registered value is 589.
        assert rb.TOOL_USE_SYSTEM_PROMPT_ALLOWANCE >= 589

    def test_a_broken_tokenizer_and_no_api_is_unavailable_and_never_fits(self):
        def broken(_text):
            raise tk.EncoderLoadError("no rank file")

        budget = rb.request_budget(_params(), use_api=False, local_counter=broken)
        assert budget.count is None and budget.count_source == rb.COUNT_SOURCE_UNAVAILABLE
        assert budget.fits is False
        assert "tokenizer failed" in budget.unavailable_reason


class TestSafetyFactorsFollowTheTokenizer:
    def test_models_on_the_opus_4_7_tokenizer_share_the_wider_pad(self):
        # Opus 4.8, Opus 5, and Sonnet 5 share the tokenizer introduced with
        # Opus 4.7 (~30% more tokens than the one before), so one factor.
        factors = {m: tk.local_estimate_safety_factor(m) for m in ("claude-opus-5", "claude-opus-4-8", MODEL_SONNET_5)}
        assert len(set(factors.values())) == 1
        assert factors[MODEL_SONNET_5] >= 1.30 * tk.local_estimate_safety_factor(MODEL_SONNET_46)

    def test_unknown_models_get_the_widest_margin(self):
        known = [tk.local_estimate_safety_factor(m) for m in tk._LOCAL_SAFETY_FACTORS]
        assert tk.local_estimate_safety_factor("claude-future-9") >= max(known)

    def test_the_numerical_note_is_derived_not_hard_coded(self):
        # Plan WP-08: under the 822,000 budget and the 1.45 factor, the
        # largest local count that still fits is about 566,897.
        limit = CROSS_CHECK_RECOMMENDED_MAX
        factor = tk.local_estimate_safety_factor(MODEL_SONNET_5)
        largest = max(n for n in range(int(limit / factor) - 5, int(limit / factor) + 5)
                      if tk.safe_local_estimate(n, model=MODEL_SONNET_5) <= limit)
        assert largest == 566_896


class TestCeiling:
    def test_phase_limit_governs_on_a_1m_window(self):
        window, reserve, ceiling = rb.input_ceiling_for(MODEL_SONNET_5, output_reserve=96_000, phase_limit=822_000)
        assert (window, reserve, ceiling) == (1_000_000, 50_000, 822_000)

    def test_the_model_ceiling_governs_on_a_small_window(self):
        _window, reserve, ceiling = rb.input_ceiling_for(MODEL_HAIKU_45, output_reserve=64_000, phase_limit=822_000)
        assert reserve == 10_000 and ceiling == 200_000 - 64_000 - 10_000

    def test_the_real_output_cap_is_subtracted(self):
        _w, _r, small = rb.input_ceiling_for(MODEL_OPUS_5, output_reserve=REVIEW_OUTPUT_CAP)
        _w, _r, large = rb.input_ceiling_for(MODEL_OPUS_5, output_reserve=REVIEW_OUTPUT_CAP_BATCH_EXTENDED)
        assert small - large == REVIEW_OUTPUT_CAP_BATCH_EXTENDED - REVIEW_OUTPUT_CAP

    def test_the_budget_records_the_whole_decision(self):
        client = CountingClient(lambda _r: 900_000)
        budget = rb.request_budget(_params(), phase_limit=822_000, client_factory=lambda: client)
        assert budget.model == MODEL_SONNET_5
        assert (budget.count, budget.count_source) == (900_000, rb.COUNT_SOURCE_API)
        assert (budget.input_ceiling, budget.output_reserve, budget.fits) == (822_000, 96_000, False)
        assert budget.unavailable_reason is None
        assert "API estimate" in budget.describe() and "exact" not in budget.describe()


# ===========================================================================
# 2. The nine WP-08 acceptance cases
# ===========================================================================


class TestAcceptance1LocalUnderButApiOver:
    """Local count under 822k, API count over the safe ceiling → chunking,
    no oversized message submission."""

    def test_chunks_and_never_sends_an_oversized_request(self, monkeypatch):
        sizes = {name: 250_000 for name in _FOUR}  # whole package ≈ 1.01M
        count_fn = sized(sizes)
        client = PassClient(count_fn=count_fn)
        _install(monkeypatch, cc, client)
        monkeypatch.setattr(cc, "count_tokens", lambda text: len(text.split()))  # tiny local

        result = cc.run_chunked_cross_check(_four_specs(), [], cycle=DEFAULT_CYCLE)

        # The raw local count would have sent one request; the estimate chunks.
        local = rb.local_request_tokens(
            rb.count_request_from_params(
                cc.build_cross_check_request(_four_specs(), [], cycle=DEFAULT_CYCLE)
            ),
            counter=lambda text: len(text.split()),
        )
        assert local < CROSS_CHECK_RECOMMENDED_MAX
        assert result.cross_check_status == "completed"
        assert client.streamed_specs() == [_FOUR[:2], _FOUR[2:]]
        for call in client.stream_calls:
            assert _count_of(client, count_fn, call) <= CROSS_CHECK_RECOMMENDED_MAX


class TestAcceptance2PreflightUnavailable:
    """Preflight unavailable → the padded estimate blocks the same unsafe case."""

    @staticmethod
    def _specs():
        # 150 markers x 1,000 = 150k local tokens a spec: 600k raw for the
        # package (under 822k) but 870k once padded for Sonnet 5.
        return [_spec(name, "ZQXW " * 150) for name in _FOUR]

    @pytest.mark.parametrize("how", ["api-down", "disabled"])
    def test_padded_estimate_chunks_the_package(self, monkeypatch, how):
        client = PassClient(count_fn=None if how == "api-down" else (lambda _r: 1))
        if how == "disabled":
            monkeypatch.setattr("src.core.api_config.token_count_preflight_enabled", lambda: False)
        _install(monkeypatch, cc, client)
        counter = lambda text: text.count("ZQXW") * 1_000  # noqa: E731
        monkeypatch.setattr(cc, "count_tokens", counter)

        result = cc.run_chunked_cross_check(self._specs(), [], cycle=DEFAULT_CYCLE)

        full = rb.local_request_tokens(
            rb.count_request_from_params(cc.build_cross_check_request(self._specs(), [], cycle=DEFAULT_CYCLE)),
            counter=counter,
        )
        assert full < CROSS_CHECK_RECOMMENDED_MAX  # the raw count would have sent it
        assert result.cross_check_status == "completed"
        assert client.streamed_specs() == [_FOUR[:2], _FOUR[2:]]
        for call in client.stream_calls:
            padded = tk.safe_local_estimate(
                rb.local_request_tokens(rb.count_request_from_params(call), counter=counter),
                model=CROSS_CHECK_MODEL_DEFAULT,
            )
            assert padded <= CROSS_CHECK_RECOMMENDED_MAX
        if how == "disabled":
            assert client.count_calls == []


class TestAcceptance3FittingApiCount:
    """Fitting API count → one request with the counted shape."""

    def test_one_request_whose_counting_form_is_what_was_counted(self, monkeypatch):
        client = PassClient(count_fn=sized({name: 1_000 for name in _FOUR}))
        _install(monkeypatch, cc, client)
        monkeypatch.setattr(cc, "count_tokens", lambda text: len(text.split()))

        result = cc.run_chunked_cross_check(_four_specs(), [], cycle=DEFAULT_CYCLE)

        assert result.cross_check_status == "completed"
        assert len(client.stream_calls) == 1
        # Counted once (the single-call path reuses the cached estimate), and
        # the counted form is exactly the sent request's counting form.
        assert len(client.count_calls) == 1
        assert client.count_calls[0] == rb.count_request_from_params(client.stream_calls[0])

    def test_the_compliance_pass_counts_what_it_sends(self, monkeypatch):
        client = PassClient(count_fn=sized({name: 1_000 for name in _FOUR}), respond=lambda _k: _compliance_ok())
        _install(monkeypatch, compliance, client)
        monkeypatch.setattr(compliance, "count_tokens", lambda text: len(text.split()))

        result = compliance.run_chunked_compliance_check(_four_specs(), _profile(), [], cycle=DEFAULT_CYCLE)

        assert result.cross_check_status == "completed"
        assert len(client.stream_calls) == 1
        assert client.count_calls == [rb.count_request_from_params(client.stream_calls[0])]


class TestAcceptance4SmallerWindowOverride:
    """Model override to a smaller window → smaller safe ceiling."""

    def test_a_200k_model_chunks_what_a_1m_model_sends_whole(self, monkeypatch):
        count_fn = sized({name: 35_000 for name in _FOUR})  # 150k for the package
        client = PassClient(count_fn=count_fn)
        _install(monkeypatch, cc, client)
        monkeypatch.setattr(cc, "count_tokens", lambda text: len(text.split()))

        cc.run_chunked_cross_check(_four_specs(), [], cycle=DEFAULT_CYCLE, model=MODEL_SONNET_5)
        assert len(client.stream_calls) == 1

        client.stream_calls.clear()
        cc.run_chunked_cross_check(_four_specs(), [], cycle=DEFAULT_CYCLE, model=MODEL_HAIKU_45)
        # Haiku: 200k window - 64k output cap - 10k reserve = 126k < 150k.
        assert client.streamed_specs() == [_FOUR[:2], _FOUR[2:]]
        haiku_ceiling = 200_000 - cross_check_max_tokens(model=MODEL_HAIKU_45) - 10_000
        for call in client.stream_calls:
            assert call["model"] == MODEL_HAIKU_45
            assert _count_of(client, count_fn, call) <= haiku_ceiling
        # The Haiku run counted for itself: no estimate crossed models.
        assert {call["model"] for call in client.count_calls} == {MODEL_SONNET_5, MODEL_HAIKU_45}


class TestAcceptance5OverheadChangesTheDecision:
    """Tool/context overhead changes → a different decision when appropriate."""

    def test_project_context_tips_the_package_into_chunks(self, monkeypatch):
        count_fn = sized({name: 200_000 for name in _FOUR}, base=5_000, marker="CTXQZ", per_marker=1_000)
        client = PassClient(count_fn=count_fn)
        _install(monkeypatch, cc, client)
        monkeypatch.setattr(cc, "count_tokens", lambda text: len(text.split()))

        cc.run_chunked_cross_check(_four_specs(), [], cycle=DEFAULT_CYCLE)
        assert len(client.stream_calls) == 1  # 805k fits

        client.stream_calls.clear()
        cc.run_chunked_cross_check(
            _four_specs(), [], cycle=DEFAULT_CYCLE, project_context="CTXQZ " * 30
        )
        assert len(client.stream_calls) == 2  # 835k does not

    def test_tool_overhead_is_part_of_the_padded_decision(self, monkeypatch):
        client = PassClient(count_fn=None)  # the fallback decides
        _install(monkeypatch, cc, client)
        counter = lambda text: text.count("ZQXW") * 1_000 + len(text.split())  # noqa: E731
        monkeypatch.setattr(cc, "count_tokens", counter)
        specs = [_spec(_FOUR[0], "ZQXW " * 283), _spec(_FOUR[1], "ZQXW " * 283)]
        with_tools_params = cc.build_cross_check_request(specs, [], cycle=DEFAULT_CYCLE)
        monkeypatch.setattr(cc, "structured_tool_output_enabled", lambda: False)
        without_tools_params = cc.build_cross_check_request(specs, [], cycle=DEFAULT_CYCLE)
        assert "tools" in with_tools_params and "tools" not in without_tools_params
        with_tools = cc.request_budget_for(with_tools_params)
        without_tools = cc.request_budget_for(without_tools_params)
        assert with_tools.count_source == rb.COUNT_SOURCE_LOCAL_PADDED
        assert with_tools.local_tokens - without_tools.local_tokens > rb.TOOL_USE_SYSTEM_PROMPT_ALLOWANCE
        # With a ceiling between the two, the tool overhead alone flips the
        # decision: the same package fits without the tool and not with it.
        monkeypatch.setattr(cc, "CROSS_CHECK_RECOMMENDED_MAX", (with_tools.count + without_tools.count) // 2)
        assert cc.request_budget_for(without_tools_params).fits
        assert not cc.request_budget_for(with_tools_params).fits


class TestAcceptance6Subdivision:
    """One oversized CSI group → deterministic subdivision; an unsplittable
    item is explicitly surfaced."""

    NAMES = [
        "23 05 00 Common.docx",
        "23 07 00 Insulation.docx",
        "23 09 00 Controls.docx",  # too large even alone
        "23 21 13 Hydronic.docx",
        "23 31 13 Ducts.docx",
    ]
    SIZES = {NAMES[0]: 200_000, NAMES[1]: 200_000, NAMES[2]: 900_000, NAMES[3]: 200_000, NAMES[4]: 200_000}

    def _specs(self):
        return [_spec(name, f"UNIQUEBODY-{index}") for index, name in enumerate(self.NAMES)]

    def test_split_into_fitting_parts_with_the_huge_spec_not_sent(self, monkeypatch):
        count_fn = sized(self.SIZES)
        client = PassClient(count_fn=count_fn)
        _install(monkeypatch, cc, client)
        monkeypatch.setattr(cc, "count_tokens", lambda text: len(text.split()))
        logs: list[tuple[str, str]] = []

        result = cc.run_chunked_cross_check(
            self._specs(), [], cycle=DEFAULT_CYCLE,
            log=lambda msg, **kw: logs.append((kw.get("level", ""), msg)),
        )

        assert result.cross_check_status == "completed"
        assert client.streamed_specs() == [self.NAMES[:2], self.NAMES[3:]]
        for call in client.stream_calls:
            assert _count_of(client, count_fn, call) <= CROSS_CHECK_RECOMMENDED_MAX
            assert "UNIQUEBODY-2" not in user_text(call)  # never sent, never truncated in
        assert result.chunk_skips == 1
        assert "(part 2 of 3)" in result.thinking
        assert f"Not analyzed: {self.NAMES[2]}" in result.thinking
        assert "nothing was truncated" in result.thinking.lower()
        assert any(level == "warning" and self.NAMES[2] in msg for level, msg in logs)

    def test_findings_from_a_split_division_keep_the_division_label(self, monkeypatch):
        finding = {
            "severity": "MEDIUM", "fileName": self.NAMES[0], "section": "2.1",
            "issue": "Common and insulation disagree on jacket color.",
            "actionType": "REPORT_ONLY", "existingText": None, "replacementText": None,
            "codeReference": None, "confidence": 0.7,
        }

        def respond(kwargs):
            names = _SPEC_NAME.findall(user_text(kwargs))
            return _cross_check_ok([finding] if self.NAMES[0] in names else [])

        client = PassClient(count_fn=sized(self.SIZES), respond=respond)
        _install(monkeypatch, cc, client)
        monkeypatch.setattr(cc, "count_tokens", lambda text: len(text.split()))

        result = cc.run_chunked_cross_check(self._specs(), [], cycle=DEFAULT_CYCLE)

        # A part of a split division is still that division: its findings
        # carry the division's label, never the pooled "Project-wide" one.
        (labelled,) = result.findings
        assert labelled.section.startswith("[Division 23 — HVAC]")

    def test_the_plan_is_deterministic(self):
        def measure(chunk_specs):
            return rb.budget_for_count(
                rb.InputCount(
                    tokens=10_000 + sum(self.SIZES[s.filename] for s in chunk_specs),
                    source=rb.COUNT_SOURCE_API,
                ),
                model=MODEL_SONNET_5, output_reserve=96_000, phase_limit=822_000,
            )

        first = plan_chunks(self._specs(), _GROUPS, measure=measure, min_specs=2, pass_name="cross-check")
        second = plan_chunks(self._specs(), _GROUPS, measure=measure, min_specs=2, pass_name="cross-check")
        shape = lambda plan: [(e.chunk_id, e.label, [s.filename for s in e.specs], e.runnable) for e in plan]  # noqa: E731
        assert shape(first) == shape(second) == [
            ("div_23:1", "Division 23 — HVAC (part 1 of 3)", self.NAMES[:2], True),
            ("div_23:2", "Division 23 — HVAC (part 2 of 3)", [self.NAMES[2]], False),
            ("div_23:3", "Division 23 — HVAC (part 3 of 3)", self.NAMES[3:], True),
        ]
        # Every spec is in exactly one chunk, in order.
        assert [s.filename for e in first for s in e.specs] == self.NAMES

    def test_a_spec_with_no_room_for_a_neighbor_is_surfaced_in_cross_check(self):
        sizes = {"23 05 00 A.docx": 700_000, "23 07 00 B.docx": 200_000, "23 09 00 C.docx": 200_000}

        def measure(chunk_specs):
            return rb.budget_for_count(
                rb.InputCount(tokens=10_000 + sum(sizes[s.filename] for s in chunk_specs), source=rb.COUNT_SOURCE_API),
                model=MODEL_SONNET_5, output_reserve=96_000, phase_limit=822_000,
            )

        specs = [_spec(name) for name in sizes]
        plan = plan_chunks(specs, _GROUPS, measure=measure, min_specs=2, pass_name="cross-check")
        assert [(e.chunk_id, [s.filename for s in e.specs], e.runnable) for e in plan] == [
            ("div_23:1", ["23 05 00 A.docx"], False),
            ("div_23:2", ["23 07 00 B.docx", "23 09 00 C.docx"], True),
        ]
        assert "only on its own" in plan[0].unanalyzed_reason
        # Compliance needs one spec per request, so the same spec runs alone.
        plan = plan_chunks(specs, _GROUPS, measure=measure, min_specs=1, pass_name="compliance")
        assert all(entry.runnable for entry in plan)

    def test_the_search_is_bounded(self):
        calls = []
        names = [f"23 {i:02d} 00 S.docx" for i in range(64)]

        def measure(chunk_specs):
            calls.append(len(chunk_specs))
            return rb.budget_for_count(
                rb.InputCount(tokens=1_000 + 100_000 * len(chunk_specs), source=rb.COUNT_SOURCE_API),
                model=MODEL_SONNET_5, output_reserve=96_000, phase_limit=822_000,
            )

        plan = plan_chunks([_spec(n) for n in names], _GROUPS, measure=measure, min_specs=2)
        assert [len(e.specs) for e in plan] == [8] * 8
        # Per part: one remainder check plus a bisection (≤ log2(64) + 1).
        assert len(calls) <= 1 + 8 * (1 + 7)


class TestAcceptance7MixedSuccess:
    """Mixed successful/failed chunks preserve all completed findings."""

    def test_completed_findings_survive_a_failed_chunk(self, monkeypatch):
        finding = {
            "severity": "MEDIUM", "fileName": _FOUR[0], "section": "2.1",
            "issue": "Water and sanitary disagree on the trap primer.",
            "actionType": "REPORT_ONLY", "existingText": None, "replacementText": None,
            "codeReference": None, "confidence": 0.7,
        }

        def respond(kwargs):
            if "23 05 00" in user_text(kwargs):
                return RuntimeError("boom")
            return _cross_check_ok([finding])

        client = PassClient(count_fn=sized({name: 250_000 for name in _FOUR}), respond=respond)
        _install(monkeypatch, cc, client)
        monkeypatch.setattr(cc, "count_tokens", lambda text: len(text.split()))
        monkeypatch.setattr(cc.time, "sleep", lambda _s: None)

        result = cc.run_chunked_cross_check(_four_specs(), [], cycle=DEFAULT_CYCLE)

        assert result.cross_check_status == "completed"
        assert [f.issue for f in result.findings] == [finding["issue"]]
        assert result.chunk_failures == 1
        # Exactly which specifications were not covered.
        assert f"Not analyzed: {_FOUR[2]}, {_FOUR[3]}" in result.thinking
        assert "within each chunk only" in result.thinking


def _review_spec(content: str, *, model: str = MODEL_OPUS_5, **overrides) -> ReviewRequestSpec:
    return ReviewRequestSpec(
        spec_content=content, filename="23 05 00 HVAC.docx", model=model,
        cycle=DEFAULT_CYCLE, **overrides,
    )


class TestAcceptance8ExtendedOutputThreshold:
    """The batch extended-output threshold uses the selected count basis;
    streaming never receives the batch-only beta."""

    @pytest.fixture(autouse=True)
    def _local(self, monkeypatch):
        monkeypatch.setattr(builder, "count_tokens", lambda text: text.count("ZQXW") * 1_000)

    def test_padded_local_crosses_where_the_raw_count_did_not(self):
        spec = _review_spec("ZQXW " * 140)  # 140k raw, 203k padded (Opus 5, 1.45)
        built = build_review_request(spec)
        assert built.input_count.source == rb.COUNT_SOURCE_LOCAL_PADDED
        assert built.input_count.local_tokens < LARGE_REVIEW_INPUT_THRESHOLD
        assert built.allow_extended_output is True
        assert built.params["max_tokens"] == REVIEW_OUTPUT_CAP_BATCH_EXTENDED

    @pytest.mark.parametrize("estimate,extended", [(180_000, False), (250_000, True)])
    def test_the_preflight_estimate_is_the_basis_when_it_exists(self, estimate, extended):
        spec = _review_spec("ZQXW " * 140)
        client = CountingClient(lambda _r: estimate)
        budget = review_request_budget(spec, use_api=True, client_factory=lambda: client)
        built = build_review_request(spec)  # no network: reads the cached estimate
        assert len(client.count_calls) == 1
        assert built.input_count.source == rb.COUNT_SOURCE_API
        assert built.input_count.tokens == estimate
        assert built.allow_extended_output is extended
        # The request that is sent carries the cap the budget was judged on.
        assert built.params["max_tokens"] == budget.output_reserve

    def test_the_realtime_transport_never_takes_the_extended_path(self):
        spec = _review_spec("ZQXW " * 300, force_allow_extended_output=False, include_service_tier=False)
        built = build_review_request(spec)
        assert built.allow_extended_output is False
        assert built.params["max_tokens"] == REVIEW_OUTPUT_CAP
        assert "betas" not in built.params and "service_tier" not in built.params
        # Its budget is judged on the cap it will really carry.
        assert review_request_budget(spec).output_reserve == REVIEW_OUTPUT_CAP

    def test_the_builder_never_calls_the_network(self, monkeypatch):
        client = CountingClient(lambda _r: 250_000)
        monkeypatch.setattr("src.review.reviewer._get_client", lambda **_: client)
        build_review_request(_review_spec("ZQXW " * 140))
        assert client.count_calls == []

    def test_realtime_jobs_are_pinned_off_the_extended_path(self):
        # Through the real job builder: even a request whose count basis is
        # far over the threshold is built on the baseline cap.
        jobs, _request_map = rt.build_realtime_review_jobs(
            [_spec("23 05 00 HVAC.docx", "ZQXW " * 300)], model=MODEL_OPUS_5
        )
        built = build_review_request(jobs[0].request_spec)
        assert built.allow_extended_output is False
        assert built.params["max_tokens"] == REVIEW_OUTPUT_CAP

    def test_the_realtime_gate_reads_the_same_basis(self, monkeypatch):
        # A cached API estimate at the threshold refuses real-time before any
        # client exists, even though the raw local count is tiny.
        spec = _spec("23 05 00 HVAC.docx", "small body")
        request = _review_spec("small body", project_context="")
        review_request_budget(request, use_api=True, client_factory=lambda: CountingClient(lambda _r: 250_000))

        def no_client(**_kwargs):
            raise AssertionError("no client before the gate")

        monkeypatch.setattr(rt, "_get_client", no_client)
        with pytest.raises(ValueError, match="batch mode"):
            rt.run_realtime_review([spec], model=MODEL_OPUS_5)


class TestAcceptance9InvalidCounts:
    """Invalid count responses never authorize an oversized call."""

    @pytest.mark.parametrize("invalid", [0, -1, None, True, "10", 2.5], ids=str)
    def test_an_invalid_estimate_falls_back_and_the_package_is_chunked(self, monkeypatch, invalid):
        client = PassClient(count_fn=lambda _r: invalid)
        _install(monkeypatch, cc, client)
        counter = lambda text: text.count("ZQXW") * 1_000  # noqa: E731
        monkeypatch.setattr(cc, "count_tokens", counter)
        specs = [_spec(name, "ZQXW " * 250) for name in _FOUR]  # 1M local: over even raw

        result = cc.run_chunked_cross_check(specs, [], cycle=DEFAULT_CYCLE)

        assert result.cross_check_status == "completed"
        assert client.streamed_specs() == [_FOUR[:2], _FOUR[2:]]
        for call in client.stream_calls:
            padded = tk.safe_local_estimate(
                rb.local_request_tokens(rb.count_request_from_params(call), counter=counter),
                model=CROSS_CHECK_MODEL_DEFAULT,
            )
            assert padded <= CROSS_CHECK_RECOMMENDED_MAX

    def test_invalid_and_no_tokenizer_sends_nothing(self, monkeypatch):
        client = PassClient(count_fn=lambda _r: 0)
        _install(monkeypatch, cc, client)

        def broken(_text):
            raise tk.EncoderLoadError("no rank file")

        monkeypatch.setattr(cc, "count_tokens", broken)
        result = cc.run_chunked_cross_check(_four_specs(), [], cycle=DEFAULT_CYCLE)
        assert result.cross_check_status == "skipped"
        assert client.stream_calls == []
        assert "could not be determined" in result.thinking


class TestTheSingleCallIsGuardedToo:
    """A direct caller of the un-chunked pass never sends an oversized request."""

    def test_run_cross_check_skips_with_the_reason(self, monkeypatch):
        client = PassClient(count_fn=lambda _r: 900_000)
        _install(monkeypatch, cc, client)
        monkeypatch.setattr(cc, "count_tokens", lambda text: len(text.split()))

        result = cc.run_cross_check(_four_specs(), [], cycle=DEFAULT_CYCLE)

        assert result.cross_check_status == "skipped"
        assert client.stream_calls == []
        assert "900,000 tokens (API estimate" in result.thinking
        assert "input ceiling of 822,000" in result.thinking
        assert "nothing was truncated" in result.thinking

    def test_run_compliance_check_skips_with_the_reason(self, monkeypatch):
        client = PassClient(count_fn=lambda _r: 900_000, respond=lambda _k: _compliance_ok())
        _install(monkeypatch, compliance, client)
        monkeypatch.setattr(compliance, "count_tokens", lambda text: len(text.split()))

        result = compliance.run_compliance_check(_four_specs(), _profile(), [], cycle=DEFAULT_CYCLE)

        assert result.cross_check_status == "skipped"
        assert client.stream_calls == []
        assert "compliance request" in result.thinking and "900,000" in result.thinking


class TestReviewPreflightUsesTheBudget:
    """The per-spec review preflight judges each spec on one basis."""

    @pytest.fixture(autouse=True)
    def _local(self, monkeypatch):
        monkeypatch.setattr(builder, "count_tokens", lambda text: text.count("ZQXW") * 1_000)

    def _prepare(self, monkeypatch, specs, *, model=MODEL_OPUS_5, client=None, log=None):
        from pathlib import Path

        from src.orchestration import pipeline

        monkeypatch.setattr(pipeline, "extract_multiple_specs_cached", lambda paths: specs)
        if client is not None:
            monkeypatch.setattr("src.review.reviewer._get_client", lambda **_: client)
        return pipeline._prepare_specs(
            input_dir=Path("."), files=[Path(s.filename) for s in specs], model=model,
            log=log or (lambda *_a, **_k: None),
        )

    def test_the_api_estimate_is_not_overruled_by_the_padded_guess(self, monkeypatch):
        # 380k raw local pads to 551k for Opus 5 — over the 500k review limit —
        # but the API estimate for the same request is 430k, which fits.
        specs = [_spec("23 05 00 HVAC.docx", "ZQXW " * 380)]
        client = CountingClient(lambda _r: 430_000)
        self._prepare(monkeypatch, specs, client=client)
        assert len(client.count_calls) == 1

    def test_the_padded_guess_decides_when_the_api_is_down(self, monkeypatch):
        specs = [_spec("23 05 00 HVAC.docx", "ZQXW " * 380)]
        with pytest.raises(ValueError) as excinfo:
            self._prepare(monkeypatch, specs, client=PassClient(count_fn=None))
        message = str(excinfo.value)
        assert "local estimate" in message and "no API estimate" in message
        assert "exact" not in message

    def test_a_fallback_is_reported_in_the_run_log(self, monkeypatch):
        specs = [_spec("23 05 00 HVAC.docx", "ZQXW " * 10)]
        logs: list[str] = []
        self._prepare(
            monkeypatch, specs, client=PassClient(count_fn=None),
            log=lambda msg, **_kw: logs.append(msg),
        )
        assert any(
            "no API estimate for 1 spec(s)" in msg and "23 05 00 HVAC.docx" in msg
            for msg in logs
        )

    def test_a_smaller_window_model_lowers_the_review_ceiling(self, monkeypatch):
        # 150k fits Opus 5's 500k review limit, not Haiku's 126k window ceiling.
        specs = [_spec("23 05 00 HVAC.docx", "body")]
        self._prepare(monkeypatch, specs, model=MODEL_OPUS_5, client=CountingClient(lambda _r: 150_000))
        with pytest.raises(ValueError, match="input ceiling of 126,000"):
            self._prepare(monkeypatch, specs, model=MODEL_HAIKU_45, client=CountingClient(lambda _r: 150_000))

    def test_every_oversized_spec_is_named_in_one_error(self, monkeypatch):
        specs = [_spec(f"23 0{i} 00 S.docx", f"body {i}") for i in range(3)]
        estimates = iter([600_000, 100_000, 700_000])
        client = CountingClient(lambda _r: next(estimates))
        with pytest.raises(ValueError) as excinfo:
            self._prepare(monkeypatch, specs, client=client)
        message = str(excinfo.value)
        assert message.startswith("2 spec(s) are too large")
        assert "23 00 00 S.docx" in message and "23 02 00 S.docx" in message
        assert "23 01 00 S.docx" not in message
