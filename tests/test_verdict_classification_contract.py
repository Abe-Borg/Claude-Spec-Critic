"""One verdict-classification contract for both verification transports.

Plan WP-10: "Real time and batch share one verdict-classification contract. A
malformed or missing verdict after an ordinary end turn is an operational
failure that keeps its known usage, not an UNVERIFIED. Refusal, max tokens,
malformed tool input, and unexpected stops are each handled explicitly."

Before the fix the two transports disagreed on the same response. Reproduced
on master ``577f578``, with a finished turn that had search evidence:

* text holding no JSON → a *grounded*, cacheable UNVERIFIED in real time, an
  operational failure on batch;
* a verdict tool call with no ``verdict`` field, or ``"PROBABLY"`` → a grounded,
  cacheable UNVERIFIED on **both** (the missing verdict was coerced);
* a verdict tool call whose input is not an object → a clean UNVERIFIED in real
  time (the call was skipped, then "no content"), a failure on batch;
* every real-time failure dropped its token usage (a ``max_tokens`` stop cost
  16k output tokens and reported zero);
* a fetch-only conversation passed the real-time evidence gate and failed the
  batch gate (which also required a non-zero search counter).

Each case below is one scripted response, run through the real
``verify_finding`` and the real batch wave loop, and every assertion is made on
both results — plus a field-by-field comparison of the two, which is what
"one contract" means. ``classify_verification_turn`` is the contract; the
transports differ only in how they pause, retry, and batch.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

import pytest

import src.verification.verifier as V
from src.orchestration.pipeline import _shareable_verdict
from src.output.report_status import ReportStatus, classify_status
from src.verification.verification_cache import VerificationCache, is_cache_eligible
from src.verification.verifier import (
    FAILURE_OUTCOMES,
    OUTCOME_CONTEXT_WINDOW,
    OUTCOME_MALFORMED_VERDICT,
    OUTCOME_MAX_TOKENS,
    OUTCOME_NO_SEARCH,
    OUTCOME_NO_VERDICT,
    OUTCOME_REFUSAL,
    OUTCOME_SEARCH_FAILED,
    OUTCOME_UNEXPECTED_STOP,
    OUTCOME_VERDICT,
    classify_verification_turn,
)
from tests.fixtures.fake_anthropic import FakeTextBlock
from tests.fixtures.verification_drivers import (
    CACHE_READ_TOKENS,
    CACHE_WRITE_TOKENS,
    FETCHED_URL,
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    SEARCHED_URL,
    failed_search_blocks,
    fetch_blocks,
    medium_finding,
    message,
    run_batch,
    run_realtime,
    search_blocks,
    verdict_call,
    verdict_payload,
)


@dataclass(frozen=True)
class Case:
    id: str
    build: Callable[[], Any]
    outcome: str
    status: ReportStatus
    grounded: bool
    cacheable: bool = False
    shareable: bool = False
    explanation_has: str = ""

    @property
    def failed(self) -> bool:
        return self.outcome in FAILURE_OUTCOMES


def _refusal_details():
    return SimpleNamespace(category="cyber", explanation="Policy declined.")


CASES = [
    # ----- well-formed verdicts ------------------------------------------
    Case(
        "confirmed",
        lambda: message([*search_blocks(), verdict_call(verdict_payload("CONFIRMED"))]),
        OUTCOME_VERDICT, ReportStatus.VERIFIED_SUPPORTED, grounded=True, cacheable=True,
    ),
    Case(
        "disputed_with_a_real_citation",
        lambda: message([*search_blocks(), verdict_call(verdict_payload("DISPUTED"))]),
        OUTCOME_VERDICT, ReportStatus.DISPUTED, grounded=True, cacheable=True,
    ),
    Case(
        # The verifier's own statement of uncertainty, grounded: genuine
        # uncertainty — never cached, shared in-process.
        "unverified_grounded",
        lambda: message([
            *search_blocks(),
            verdict_call(verdict_payload("UNVERIFIED", source_quote=None)),
        ]),
        OUTCOME_VERDICT, ReportStatus.INSUFFICIENT_EVIDENCE, grounded=True, shareable=True,
    ),
    Case(
        # A well-formed verdict the quote rule demotes stays a verdict.
        "confirmed_without_a_quote",
        lambda: message([*search_blocks(), verdict_call(verdict_payload("CONFIRMED", source_quote=""))]),
        OUTCOME_VERDICT, ReportStatus.INSUFFICIENT_EVIDENCE, grounded=True, shareable=True,
        explanation_has="source_quote was empty",
    ),
    Case(
        # Blank citations are rejected on both transports: the DISPUTED is
        # demoted, not counted as disputed.
        "disputed_backed_only_by_blank_sources",
        lambda: message([
            *search_blocks(),
            verdict_call(verdict_payload("DISPUTED", sources=["", "   "])),
        ]),
        OUTCOME_VERDICT, ReportStatus.INSUFFICIENT_EVIDENCE, grounded=False, shareable=True,
    ),
    Case(
        "text_fallback_json_verdict",
        lambda: message(
            [*search_blocks(), FakeTextBlock(text=_json(verdict_payload("CONFIRMED")))],
            stop_reason="end_turn",
        ),
        OUTCOME_VERDICT, ReportStatus.VERIFIED_SUPPORTED, grounded=True, cacheable=True,
    ),
    Case(
        # Fetch-only evidence clears the gate on both transports (the batch
        # gate used to require a search).
        "fetch_only_confirmed",
        lambda: message(
            [*fetch_blocks(), verdict_call(verdict_payload("CONFIRMED", sources=[FETCHED_URL]))],
            searches=0, fetches=1,
        ),
        OUTCOME_VERDICT, ReportStatus.VERIFIED_SUPPORTED, grounded=True, cacheable=True,
    ),
    # ----- malformed or missing verdicts after a finished turn -----------
    Case(
        "tool_call_without_a_verdict_field",
        lambda: message([*search_blocks(), verdict_call({k: v for k, v in verdict_payload().items() if k != "verdict"})]),
        OUTCOME_MALFORMED_VERDICT, ReportStatus.VERIFICATION_FAILED, grounded=False,
        explanation_has="no 'verdict' field",
    ),
    Case(
        "tool_call_with_an_unknown_verdict",
        lambda: message([*search_blocks(), verdict_call(verdict_payload("PROBABLY"))]),
        OUTCOME_MALFORMED_VERDICT, ReportStatus.VERIFICATION_FAILED, grounded=False,
        explanation_has="'PROBABLY'",
    ),
    Case(
        "tool_call_with_a_null_verdict",
        lambda: message([*search_blocks(), verdict_call(verdict_payload(None))]),
        OUTCOME_MALFORMED_VERDICT, ReportStatus.VERIFICATION_FAILED, grounded=False,
    ),
    Case(
        "tool_input_that_is_not_an_object",
        lambda: message([*search_blocks(), verdict_call('{"verdict": "CONF')]),
        OUTCOME_MALFORMED_VERDICT, ReportStatus.VERIFICATION_FAILED, grounded=False,
        explanation_has="not an object",
    ),
    Case(
        "two_verdict_calls_that_disagree",
        lambda: message([
            *search_blocks(),
            verdict_call(verdict_payload("CONFIRMED"), block_id="toolu_a"),
            verdict_call(verdict_payload("DISPUTED"), block_id="toolu_b"),
        ]),
        OUTCOME_MALFORMED_VERDICT, ReportStatus.VERIFICATION_FAILED, grounded=False,
        explanation_has="conflicting verdicts",
    ),
    Case(
        "text_without_json",
        lambda: message(
            [*search_blocks(), FakeTextBlock(text="I looked it up and it seems fine.")],
            stop_reason="end_turn",
        ),
        OUTCOME_MALFORMED_VERDICT, ReportStatus.VERIFICATION_FAILED, grounded=False,
        explanation_has="did not contain structured JSON",
    ),
    Case(
        "text_json_without_a_verdict",
        lambda: message(
            [*search_blocks(), FakeTextBlock(text='{"explanation": "fine", "sources": []}')],
            stop_reason="end_turn",
        ),
        OUTCOME_MALFORMED_VERDICT, ReportStatus.VERIFICATION_FAILED, grounded=False,
        explanation_has="held no valid verdict",
    ),
    Case(
        # The missing expected tool output: evidence, a finished turn, and
        # nothing submitted.
        "end_turn_with_no_verdict_and_no_text",
        lambda: message(search_blocks(), stop_reason="end_turn"),
        OUTCOME_NO_VERDICT, ReportStatus.VERIFICATION_FAILED, grounded=False,
        explanation_has="without submitting a verdict",
    ),
    Case(
        "tool_use_stop_without_a_verdict_call",
        lambda: message(search_blocks(), stop_reason="tool_use"),
        OUTCOME_NO_VERDICT, ReportStatus.VERIFICATION_FAILED, grounded=False,
    ),
    # ----- no evidence ----------------------------------------------------
    Case(
        "no_search_at_all",
        lambda: message([verdict_call(verdict_payload("CONFIRMED"))], searches=0),
        OUTCOME_NO_SEARCH, ReportStatus.VERIFICATION_FAILED, grounded=False,
        explanation_has="did not perform web search",
    ),
    Case(
        "every_search_errored",
        lambda: message([*failed_search_blocks(), verdict_call(verdict_payload("CONFIRMED"))], searches=1),
        OUTCOME_SEARCH_FAILED, ReportStatus.VERIFICATION_FAILED, grounded=False,
        explanation_has="all 1 search requests failed",
    ),
    # ----- incomplete stops, each named -----------------------------------
    Case(
        "refusal",
        lambda: message(search_blocks(), stop_reason="refusal", stop_details=_refusal_details()),
        OUTCOME_REFUSAL, ReportStatus.VERIFICATION_FAILED, grounded=False,
        explanation_has="refused by the model (stop_reason: refusal, category: cyber): Policy declined.",
    ),
    Case(
        "max_tokens",
        lambda: message([*search_blocks(), FakeTextBlock(text="Checking… (cut")], stop_reason="max_tokens"),
        OUTCOME_MAX_TOKENS, ReportStatus.VERIFICATION_FAILED, grounded=False,
        explanation_has="ran out of output tokens",
    ),
    Case(
        "context_window_exceeded",
        lambda: message(search_blocks(), stop_reason="model_context_window_exceeded"),
        OUTCOME_CONTEXT_WINDOW, ReportStatus.VERIFICATION_FAILED, grounded=False,
        explanation_has="context window",
    ),
    Case(
        "stop_sequence",
        lambda: message([*search_blocks(), verdict_call(verdict_payload("CONFIRMED"))], stop_reason="stop_sequence"),
        OUTCOME_UNEXPECTED_STOP, ReportStatus.VERIFICATION_FAILED, grounded=False,
        explanation_has="stop_reason: stop_sequence",
    ),
    Case(
        "no_stop_reason",
        lambda: message([*search_blocks(), verdict_call(verdict_payload("CONFIRMED"))], stop_reason=None),
        OUTCOME_UNEXPECTED_STOP, ReportStatus.VERIFICATION_FAILED, grounded=False,
    ),
    Case(
        "a_stop_reason_this_app_does_not_know",
        lambda: message(search_blocks(), stop_reason="some_future_stop"),
        OUTCOME_UNEXPECTED_STOP, ReportStatus.VERIFICATION_FAILED, grounded=False,
        explanation_has="stop_reason: some_future_stop",
    ),
]


def _json(payload: dict) -> str:
    import json

    return json.dumps(payload)


# Every field the contract decides. Real time and batch must agree on each
# for the same response; the transports may differ only in what their own
# loops add (none of these).
_CONTRACT_FIELDS = (
    "verdict",
    "outcome",
    "verification_failed",
    "budget_exhausted",
    "grounded",
    "explanation",
    "sources",
    "cited_sources",
    "accepted_sources",
    "rejected_sources",
    "rejected_source_reasons",
    "searched_sources",
    "fetched_sources",
    "source_quote",
    "correction",
    "web_search_requests",
    "web_fetch_requests",
    "successful_source_count",
    "search_error_count",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens",
    "cache_creation_unknown_input_tokens",
    "cache_creation_breakdown_status",
    "model_used",
    "verification_mode",
    "verification_profile",
    "escalated",
    "cache_status",
    "structured_payload",
)


def _both(monkeypatch, case: Case):
    rt_cache, bt_cache = VerificationCache(), VerificationCache()
    rt, client = run_realtime(monkeypatch, case.build(), cache=rt_cache)
    assert len(client.calls) == 1, "one streaming call, no retry, no escalation"
    bt_finding = run_batch(monkeypatch, case.build(), cache=bt_cache)
    return (rt, rt_cache), (bt_finding.verification, bt_cache)


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
class TestOneContractForBothTransports:
    def test_each_transport_classifies_the_response_as_the_contract_says(self, monkeypatch, case):
        for label, (result, cache) in zip(("realtime", "batch"), _both(monkeypatch, case)):
            assert result.outcome == case.outcome, label
            assert result.verification_failed is case.failed, label
            assert result.grounded is case.grounded, label
            finding = medium_finding()
            finding.verification = result
            assert classify_status(finding) is case.status, label
            if case.explanation_has:
                assert case.explanation_has in result.explanation, (label, result.explanation)
            # A failure never looks like the verifier's own uncertainty.
            if case.failed:
                assert result.verdict == "UNVERIFIED", label
                assert result.budget_exhausted is False, label
                telemetry = result.retry_telemetry or {}
                assert telemetry.get("terminal_reason") == case.outcome, label
                assert telemetry.get("failure_class") == "parse_error", label
                assert telemetry.get("attempts") == 1, label

    def test_the_known_usage_survives_on_both_transports(self, monkeypatch, case):
        """A failure is not free just because no verdict parsed (WP-10 item 3)."""
        for label, (result, _cache) in zip(("realtime", "batch"), _both(monkeypatch, case)):
            assert (result.input_tokens, result.output_tokens) == (INPUT_TOKENS, OUTPUT_TOKENS), label
            assert result.cache_creation_input_tokens == CACHE_WRITE_TOKENS, label
            assert result.cache_read_input_tokens == CACHE_READ_TOKENS, label

    def test_only_a_grounded_conclusive_verdict_reaches_the_cache(self, monkeypatch, case):
        for label, (result, cache) in zip(("realtime", "batch"), _both(monkeypatch, case)):
            assert is_cache_eligible(result) is case.cacheable, label
            assert cache.stats()["size"] == (1 if case.cacheable else 0), label
            replay = cache.get(medium_finding(), cycle=V.DEFAULT_CYCLE)
            assert (replay is not None) is case.cacheable, label

    def test_only_a_well_formed_unverified_may_be_shared_in_process(self, monkeypatch, case):
        for label, (result, _cache) in zip(("realtime", "batch"), _both(monkeypatch, case)):
            assert _shareable_verdict(result) is case.shareable, label

    def test_the_two_transports_agree_field_by_field(self, monkeypatch, case):
        (rt, _), (bt, _) = _both(monkeypatch, case)
        mismatched = {
            name: (getattr(rt, name), getattr(bt, name))
            for name in _CONTRACT_FIELDS
            if getattr(rt, name) != getattr(bt, name)
        }
        assert mismatched == {}


class TestTheContractItself:
    def test_the_case_table_covers_every_turn_outcome(self):
        """The table exercises every outcome ``classify_verification_turn`` can return."""
        turn_outcomes = {
            OUTCOME_VERDICT,
            OUTCOME_REFUSAL,
            OUTCOME_MAX_TOKENS,
            OUTCOME_CONTEXT_WINDOW,
            OUTCOME_UNEXPECTED_STOP,
            OUTCOME_NO_SEARCH,
            OUTCOME_SEARCH_FAILED,
            OUTCOME_NO_VERDICT,
            OUTCOME_MALFORMED_VERDICT,
        }
        assert {case.outcome for case in CASES} == turn_outcomes

    def test_a_paused_turn_is_continued_not_classified(self):
        paused = message(search_blocks(), stop_reason="pause_turn")
        with pytest.raises(ValueError):
            classify_verification_turn(
                paused, evidence=V._collect_conversation_evidence([paused]), parse_messages=[paused]
            )

    def test_a_malformed_verdict_is_never_coerced_to_unverified(self):
        """The parser no longer turns an unknown verdict into UNVERIFIED."""
        parse = V.parse_verification_response(
            message([*search_blocks(), verdict_call(verdict_payload("MAYBE"))])
        )
        assert parse.verdict is None
        assert parse.parse_status == V.PARSE_STATUS_MALFORMED

    def test_case_and_whitespace_in_a_verdict_are_forgiven(self):
        parse = V.parse_verification_response(
            message([*search_blocks(), verdict_call(verdict_payload(" disputed "))])
        )
        assert parse.parse_status == V.PARSE_STATUS_STRUCTURED
        assert parse.verdict.verdict == "DISPUTED"

    def test_blank_citations_stay_visible_as_rejected_on_both_transports(self, monkeypatch):
        """Rejected, not dropped: the evidence panel still shows them as empty."""
        build = CASES[[c.id for c in CASES].index("disputed_backed_only_by_blank_sources")].build
        rt, _ = run_realtime(monkeypatch, build(), cache=None)
        bt = run_batch(monkeypatch, build()).verification
        for label, result in (("realtime", rt), ("batch", bt)):
            assert result.accepted_sources == [] and result.sources == [], label
            assert {r["reason"] for r in result.rejected_sources} == {"empty"}, label

    def test_the_searched_url_is_recorded_on_a_failure(self, monkeypatch):
        """Safely captured evidence rides along on a failure (WP-10 item 3)."""
        msg = message([*search_blocks(), verdict_call(verdict_payload("PROBABLY"))])
        rt, _ = run_realtime(monkeypatch, msg, cache=None)
        bt = run_batch(monkeypatch, msg).verification
        for label, result in (("realtime", rt), ("batch", bt)):
            assert result.searched_sources == [SEARCHED_URL], label
            assert result.successful_source_count == 1, label
            assert result.web_search_requests == 2, label


class TestFailuresAreBilled:
    """A failed parse keeps its usage all the way into the cost summary."""

    @pytest.mark.parametrize(
        "case_id",
        ["tool_call_with_an_unknown_verdict", "text_without_json", "max_tokens", "refusal", "no_search_at_all"],
    )
    def test_the_failure_is_a_priced_call_on_both_transports(self, monkeypatch, case_id):
        from src.orchestration.diagnostics import DiagnosticsReport, record_verification_findings

        build = CASES[[c.id for c in CASES].index(case_id)].build
        rt, _client = run_realtime(monkeypatch, build())
        bt = run_batch(monkeypatch, build()).verification
        for transport, result in (("realtime", rt), ("batch", bt)):
            assert result.verification_failed is True, transport
            finding = medium_finding()
            finding.verification = result
            diag = DiagnosticsReport()
            record_verification_findings(diag, [finding], phase="verification", transport=transport)
            summary = diag.summary()
            phase = summary["phase_telemetry"]["verification"]
            assert phase["calls"] == 1, transport
            assert (phase["input_tokens"], phase["output_tokens"]) == (INPUT_TOKENS, OUTPUT_TOKENS), transport
            estimate = summary["cost_summary"]["estimated_cost_usd"]
            assert estimate["priced_calls"] == 1 and estimate["total"] > 0, transport


class TestTheEdgesOfTheContract:
    def test_the_grounding_invariant_itself_rejects_blank_sources(self):
        """The invariant's own check, for a result built without the grounding
        step (``_apply_source_grounding`` already rejects blank citations, so
        the transports reach the invariant with clean lists)."""
        result = V.VerificationResult(
            verdict="DISPUTED", grounded=True, sources=["   "], accepted_sources=[""]
        )
        V._enforce_grounding_invariant(result)
        assert result.verdict == "UNVERIFIED"
        assert result.grounded is False

    def test_a_dict_shaped_batch_message_parses_its_text_verdict(self, monkeypatch):
        """The batch results stream may hand back plain dicts; their text blocks
        are read like SDK blocks (the view of an earlier wave is dicts too)."""
        from tests.fixtures.fake_anthropic import _to_dict

        msg = _to_dict(
            message(
                [*search_blocks(), FakeTextBlock(text=_json(verdict_payload("CONFIRMED")))],
                stop_reason="end_turn",
            )
        )
        result = run_batch(monkeypatch, SimpleNamespace(**msg)).verification
        assert result.outcome == OUTCOME_VERDICT
        assert result.verdict == "CONFIRMED"

    def test_a_failed_escalation_never_replaces_the_first_pass(self):
        initial = V.VerificationResult(verdict="UNVERIFIED", grounded=True, sources=[SEARCHED_URL])
        failed_escalation = V.VerificationResult(
            verdict="CONFIRMED",
            grounded=True,
            sources=[SEARCHED_URL],
            verification_failed=True,
        )
        kept = V._apply_escalation_outcome(
            initial_result=initial,
            esc_result=failed_escalation,
            initial_verdict="UNVERIFIED",
            initial_model="claude-sonnet-5",
            initial_grounded=True,
            initial_sources=[SEARCHED_URL],
            escalation_reason="initial_unverified",
        )
        assert kept is initial
        assert kept.verification_failed is False

    def test_no_api_key_is_an_operational_failure(self, monkeypatch):
        from src.verification.verifier import OUTCOME_NO_API_KEY

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result, client = run_realtime(monkeypatch, message(search_blocks()))
        assert client.calls == []
        assert result.verification_failed is True
        assert result.outcome == OUTCOME_NO_API_KEY


class TestLoopTerminalsKeepTheirUsage:
    """The continuation cap is a budget terminal on both transports: not a
    failure, never shared, and it keeps the usage of every paused turn."""

    def test_realtime(self, monkeypatch):
        from src.verification.verifier import OUTCOME_CONTINUATION_CAP
        from tests.fixtures.fake_anthropic import pause_turn_response

        pause = pause_turn_response(searched_urls=[SEARCHED_URL], web_search_requests=1)
        result, client = run_realtime(monkeypatch, pause)
        assert result.outcome == OUTCOME_CONTINUATION_CAP
        assert result.verification_failed is False
        assert result.input_tokens == 100 * len(client.calls)
        assert result.web_search_requests == len(client.calls)
        assert _shareable_verdict(result) is False
        telemetry = result.retry_telemetry or {}
        assert telemetry.get("failure_class") == "pause_turn"
        assert telemetry.get("continuation_count") == len(client.calls)

    def test_batch(self, monkeypatch):
        from src.verification.verifier import OUTCOME_CONTINUATION_CAP
        from tests.fixtures.fake_anthropic import pause_turn_response

        pause = pause_turn_response(searched_urls=[SEARCHED_URL], web_search_requests=1)
        result = run_batch(monkeypatch, pause, max_waves=6).verification
        waves = (result.retry_telemetry or {}).get("continuation_count")
        assert result.outcome == OUTCOME_CONTINUATION_CAP
        assert result.verification_failed is False
        assert waves and result.input_tokens == 100 * waves
        assert result.web_search_requests == waves
        assert _shareable_verdict(result) is False

    def test_a_detached_poll_leaves_an_operational_failure(self, monkeypatch):
        """The exactly-once safety net: nothing was checked, so VERIFICATION_FAILED."""
        from src.verification.verifier import OUTCOME_NO_RESULT

        monkeypatch.setattr(
            V,
            "poll_batch_bounded",
            lambda batch_id, **_kw: SimpleNamespace(detached=True, poll_failed=False),
        )
        finding = medium_finding()
        job = SimpleNamespace(batch_id="b", request_map={"verify__0": {"finding_idx": 0}}, job_type="verify")
        V.collect_verification_batch_results(
            job, [finding], cycle=V.DEFAULT_CYCLE, poll_policy=V.DEFAULT_VERIFICATION_POLL_POLICY
        )
        assert finding.verification.outcome == OUTCOME_NO_RESULT
        assert finding.verification.verification_failed is True
        assert classify_status(finding) is ReportStatus.VERIFICATION_FAILED
        assert _shareable_verdict(finding.verification) is False
