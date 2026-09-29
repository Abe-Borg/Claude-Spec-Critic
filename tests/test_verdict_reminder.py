"""One reminder to submit, for a turn that ended without its answer.

Anthropic's Opus 5.5 prompting guide ("Unattended agentic runs", checked
2026-09-29): some progress reports end the turn with text rather than a tool
call; treat such a turn as a report, not proof the work is done, send one
short user message naming what is still owed, and bound the continuations.

Before this, on master ``9fdba29``:

* a verification conversation that searched and then ended its turn with a
  text note (or nothing) instead of ``submit_verification_verdict`` was a
  terminal ``malformed_verdict`` / ``no_verdict`` failure, after its searches
  were paid for, on both transports;
* a research dimension that ended its turn without calling
  ``submit_requirements_research`` failed after up to 24 paid searches.

Now each gets exactly one reminder, appended to the same conversation. The
verifier sends it inline in real time and in the next wave in batch (when a
wave is left); neither takes a resume from the continuation budget. The
single-response cases are in ``test_verdict_classification_contract.py``.
"""
from __future__ import annotations

import copy

import pytest

import src.verification.verifier as V
from src.orchestration.diagnostics import DiagnosticsReport, record_verification_findings
from src.research.requirements_research import (
    RESEARCH_SUBMIT_REMINDER,
    ResearchFanoutError,
    run_requirements_research,
)
from src.verification.verifier import (
    OUTCOME_MALFORMED_VERDICT,
    OUTCOME_NO_SEARCH,
    OUTCOME_NO_VERDICT,
    OUTCOME_VERDICT,
    VERDICT_REMINDER_JSON,
    VERDICT_REMINDER_TOOL,
    verdict_reminder_applies,
)
from tests.fixtures.fake_anthropic import (
    FakeContainer,
    FakeMessage,
    FakeTextBlock,
    FakeThinkingBlock,
    FakeToolUseBlock,
    research_tool_use_response,
)
from tests.fixtures.preserved_thinking import preserved_thinking_violations
from tests.fixtures.verification_drivers import (
    INPUT_TOKENS,
    SEARCHED_URL,
    medium_finding,
    message,
    run_batch,
    run_realtime,
    search_blocks,
    verdict_call,
    verdict_payload,
)
from tests.test_requirements_research import (
    FakeResearchClient,
    _complete_profile,
    _enabled_module,
    _route_by_marker,
)


def _note(text: str = "I found the governing section; submitting next.") -> FakeTextBlock:
    return FakeTextBlock(text=text)


def _silent_turn(**kwargs):
    """Searched, then ended the turn with a progress note and no verdict."""
    return message([*search_blocks(), _note()], stop_reason="end_turn", **kwargs)


def _verdict_turn(verdict: str = "CONFIRMED"):
    return message([verdict_call(verdict_payload(verdict))], searches=0)


def _sequence(*messages_, bodies: list | None = None):
    """A real-time route answering calls in order.

    ``bodies``, when given, receives a copy of each request as sent: the
    loop appends to its ``messages`` list in place, so the client's own
    record of an earlier call shows the later messages too.
    """
    remaining = list(messages_)

    def route(kwargs):
        if bodies is not None:
            bodies.append(copy.deepcopy(kwargs))
        if not remaining:
            raise AssertionError("more calls than scripted")
        return remaining.pop(0)

    return route


# ---------------------------------------------------------------------------
# Verifier, real time
# ---------------------------------------------------------------------------


class TestRealtimeReminder:
    def test_a_silent_turn_gets_one_reminder_and_the_verdict_counts(self, monkeypatch):
        result, client = run_realtime(monkeypatch, _sequence(_silent_turn(), _verdict_turn()))
        assert len(client.calls) == 2
        assert result.outcome == OUTCOME_VERDICT
        assert result.verdict == "CONFIRMED"
        assert result.verification_failed is False
        assert result.verdict_reminder_sent is True
        # Grounded on the search the first turn made.
        assert result.grounded is True
        assert result.accepted_sources == [SEARCHED_URL]

    def test_the_reminder_is_appended_never_an_edit(self, monkeypatch):
        first = _silent_turn()
        bodies: list = []
        run_realtime(monkeypatch, _sequence(first, _verdict_turn(), bodies=bodies))
        initial, reminded = bodies
        assert len(initial["messages"]) == 1
        assert reminded["system"] == initial["system"]
        assert reminded["tools"] == initial["tools"]
        msgs = reminded["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
        assert msgs[0] == initial["messages"][0]
        assert msgs[1]["content"] == first.content
        assert msgs[2]["content"] == VERDICT_REMINDER_TOOL

    def test_preserved_thinking_stays_valid(self, monkeypatch):
        first = message(
            [FakeThinkingBlock(thinking="", signature="sig-1"), *search_blocks(), _note()],
            stop_reason="end_turn",
        )
        second = message(
            [FakeThinkingBlock(thinking="", signature="sig-2"), verdict_call(verdict_payload())],
            searches=0,
        )
        bodies: list = []
        run_realtime(monkeypatch, _sequence(first, second, bodies=bodies))
        exchanges = [(bodies[0], first.content), (bodies[1], second.content)]
        assert preserved_thinking_violations(exchanges) == []

    def test_the_container_is_named_on_the_reminder(self, monkeypatch):
        first = _silent_turn()
        first.container = FakeContainer(id="container_abc")
        _result, client = run_realtime(monkeypatch, _sequence(first, _verdict_turn()))
        assert "container" not in client.calls[0]
        assert client.calls[1]["container"] == "container_abc"

    def test_only_one_reminder(self, monkeypatch):
        result, client = run_realtime(
            monkeypatch, _sequence(_silent_turn(), _silent_turn(), _verdict_turn())
        )
        assert len(client.calls) == 2
        assert result.outcome == OUTCOME_MALFORMED_VERDICT
        assert result.verification_failed is True
        assert result.verdict_reminder_sent is True

    def test_the_reminder_takes_no_resume_from_the_continuation_budget(self, monkeypatch):
        cap = V.select_routing(
            medium_finding(), escalated=False, local_skip=False
        ).max_continuations
        pauses = [message(search_blocks(), stop_reason="pause_turn") for _ in range(cap)]
        result, client = run_realtime(
            monkeypatch, _sequence(*pauses, _silent_turn(), _verdict_turn())
        )
        assert len(client.calls) == cap + 2
        assert result.outcome == OUTCOME_VERDICT
        assert result.retry_telemetry is None or result.retry_telemetry.get(
            "terminal_reason"
        ) is None

    def test_a_reminded_conversation_is_one_attempt_billed_for_both_calls(self, monkeypatch):
        result, _client = run_realtime(monkeypatch, _sequence(_silent_turn(), _verdict_turn()))
        assert len(result.call_usage) == 1
        assert result.call_usage[0]["input_tokens"] == 2 * INPUT_TOKENS

    def test_the_json_wording_when_the_request_has_no_verdict_tool(self, monkeypatch):
        # The rollback path that sends no verdict tool asks for the JSON
        # object instead (the routing decision reads this switch lazily).
        monkeypatch.setattr(
            "src.batch.batch.verification_request_includes_verdict_tool", lambda: False
        )
        assert not V.select_routing(
            medium_finding(), escalated=False, local_skip=False
        ).include_verdict_tool
        text_verdict = message(
            [FakeTextBlock(text=(
                '{"verdict": "CONFIRMED", "explanation": "x", '
                f'"sources": ["{SEARCHED_URL}"], "source_quote": "q", "correction": null}}'
            ))],
            stop_reason="end_turn",
            searches=0,
        )
        bodies: list = []
        result, _client = run_realtime(
            monkeypatch, _sequence(_silent_turn(), text_verdict, bodies=bodies)
        )
        assert bodies[1]["messages"][-1]["content"] == VERDICT_REMINDER_JSON
        assert result.outcome == OUTCOME_VERDICT


class TestWhatIsNotReminded:
    @pytest.mark.parametrize(
        "build, outcome",
        [
            # A garbled submission: something was submitted; the failure stands.
            (lambda: message([*search_blocks(), verdict_call(verdict_payload("PROBABLY"))]),
             OUTCOME_MALFORMED_VERDICT),
            (lambda: message(
                [*search_blocks(), FakeTextBlock(text='{"explanation": "fine", "sources": []}')],
                stop_reason="end_turn",
            ), OUTCOME_MALFORMED_VERDICT),
            # No search: a different failure, not a missing submission.
            (lambda: message([_note()], stop_reason="end_turn", searches=0), OUTCOME_NO_SEARCH),
            # A client tool call needs its tool_result before any user text.
            (lambda: message(
                [*search_blocks(), FakeToolUseBlock(name="submit_verdict", input={})],
            ), OUTCOME_NO_VERDICT),
        ],
        ids=["malformed_call", "json_without_verdict", "no_search", "other_tool_call"],
    )
    def test_no_reminder(self, monkeypatch, build, outcome):
        result, client = run_realtime(monkeypatch, build())
        assert len(client.calls) == 1
        assert result.outcome == outcome
        assert result.verdict_reminder_sent is False
        bt = run_batch(monkeypatch, build(), max_waves=2).verification
        assert bt.outcome == outcome
        assert bt.verdict_reminder_sent is False

    def test_an_incomplete_stop_is_not_reminded(self, monkeypatch):
        cut = message([*search_blocks(), _note("Checking… (cut")], stop_reason="max_tokens")
        result, client = run_realtime(monkeypatch, cut)
        assert len(client.calls) == 1
        assert result.verdict_reminder_sent is False

    def test_the_predicate(self):
        silent = _silent_turn()
        turn = V.classify_verification_turn(
            silent,
            evidence=V._collect_conversation_evidence([silent]),
            parse_messages=[silent],
        )
        assert verdict_reminder_applies(silent, turn) is True
        answered = message([*search_blocks(), verdict_call(verdict_payload())])
        turn = V.classify_verification_turn(
            answered,
            evidence=V._collect_conversation_evidence([answered]),
            parse_messages=[answered],
        )
        assert verdict_reminder_applies(answered, turn) is False


# ---------------------------------------------------------------------------
# Verifier, batch
# ---------------------------------------------------------------------------


def _wave_route(*by_wave):
    """A batch route answering by wave: the initial id, then follow-ups."""

    def route(custom_id: str):
        for wave in range(len(by_wave) - 1, 0, -1):
            if custom_id.startswith(f"verify_cont_{wave}__"):
                return by_wave[wave]
        return by_wave[0]

    return route


class TestBatchReminder:
    def test_the_next_wave_carries_the_reminder_and_the_verdict_counts(self, monkeypatch):
        submitted: list = []
        first = _silent_turn()
        finding = run_batch(
            monkeypatch,
            _wave_route(first, _verdict_turn()),
            max_waves=2,
            submitted=submitted,
        )
        result = finding.verification
        assert result.outcome == OUTCOME_VERDICT
        assert result.verdict == "CONFIRMED"
        assert result.verdict_reminder_sent is True
        assert result.accepted_sources == [SEARCHED_URL]
        (requests, request_map), = submitted
        (request,) = requests
        assert request_map[request["custom_id"]]["type"] == "reminder"
        msgs = request["params"]["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
        assert msgs[2]["content"] == VERDICT_REMINDER_TOOL
        assert len(msgs[1]["content"]) == len(first.content)

    def test_no_wave_left_means_the_failure_stands(self, monkeypatch):
        finding = run_batch(monkeypatch, _silent_turn(), max_waves=1)
        assert finding.verification.outcome == OUTCOME_MALFORMED_VERDICT
        assert finding.verification.verdict_reminder_sent is False

    def test_a_pause_before_the_silent_turn_keeps_every_block_before_the_reminder(self, monkeypatch):
        submitted: list = []
        paused = message(search_blocks(), stop_reason="pause_turn")
        silent = message([_note()], stop_reason="end_turn", searches=0)
        finding = run_batch(
            monkeypatch,
            _wave_route(paused, silent, _verdict_turn()),
            max_waves=3,
            submitted=submitted,
        )
        assert finding.verification.outcome == OUTCOME_VERDICT
        reminder_requests, _map = submitted[1]
        msgs = reminder_requests[0]["params"]["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
        assert len(msgs[1]["content"]) == len(paused.content) + len(silent.content)

    def test_a_reminded_conversation_that_pauses_resumes_after_the_reminder(self, monkeypatch):
        submitted: list = []
        silent = _silent_turn()
        paused_after = message([_note("Checking one more source.")], stop_reason="pause_turn", searches=0)
        finding = run_batch(
            monkeypatch,
            _wave_route(silent, paused_after, _verdict_turn()),
            max_waves=3,
            submitted=submitted,
        )
        assert finding.verification.outcome == OUTCOME_VERDICT
        assert finding.verification.verdict_reminder_sent is True
        resume_requests, resume_map = submitted[1]
        assert resume_map[resume_requests[0]["custom_id"]]["type"] == "continuation"
        msgs = resume_requests[0]["params"]["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
        assert len(msgs[1]["content"]) == len(silent.content)
        assert msgs[2]["content"] == VERDICT_REMINDER_TOOL
        assert len(msgs[3]["content"]) == len(paused_after.content)

    def test_the_reminder_takes_no_resume_from_the_continuation_budget(self, monkeypatch):
        cap = V.select_routing(
            medium_finding(), escalated=False, local_skip=False
        ).max_continuations
        assert cap == 2, "the scenario below is sized for the default cap"
        # pause (1 of 2), silent turn -> reminder, pause (2 of 2), verdict. If
        # the reminder counted as a pause, the second pause would be the third
        # and hit the cap.
        finding = run_batch(
            monkeypatch,
            _wave_route(
                message(search_blocks(), stop_reason="pause_turn"),
                message([_note()], stop_reason="end_turn", searches=0),
                message([_note("One more source.")], stop_reason="pause_turn", searches=0),
                _verdict_turn(),
            ),
            max_waves=4,
        )
        assert finding.verification.outcome == OUTCOME_VERDICT
        assert finding.verification.verdict_reminder_sent is True

    def test_the_transports_agree_on_a_recovered_verdict(self, monkeypatch):
        rt, _client = run_realtime(monkeypatch, _sequence(_silent_turn(), _verdict_turn()))
        bt = run_batch(
            monkeypatch, _wave_route(_silent_turn(), _verdict_turn()), max_waves=2
        ).verification
        for field in (
            "verdict", "outcome", "grounded", "accepted_sources", "searched_sources",
            "web_search_requests", "input_tokens", "output_tokens", "verdict_reminder_sent",
        ):
            assert getattr(rt, field) == getattr(bt, field), field


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


class TestDiagnostics:
    def test_reminders_are_counted_and_worded(self, monkeypatch):
        recovered, _ = run_realtime(monkeypatch, _sequence(_silent_turn(), _verdict_turn()))
        lost, _ = run_realtime(monkeypatch, _sequence(_silent_turn(), _silent_turn()))
        plain, _ = run_realtime(monkeypatch, _verdict_turn_with_search())
        findings = []
        for result in (recovered, lost, plain):
            finding = medium_finding()
            finding.verification = result
            findings.append(finding)
        diag = DiagnosticsReport()
        record_verification_findings(diag, findings, phase="verification", transport="realtime")
        stats = diag.summary()["verification_evidence"]
        assert stats["verdict_reminders"] == 2
        assert stats["verdict_reminders_recovered"] == 1
        assert "Verdict reminders: 2 finding(s) got one" in diag.to_text()

    def test_an_event_without_a_reminder_is_unchanged(self, monkeypatch):
        plain, _ = run_realtime(monkeypatch, _verdict_turn_with_search())
        finding = medium_finding()
        finding.verification = plain
        diag = DiagnosticsReport()
        record_verification_findings(diag, [finding], phase="verification", transport="realtime")
        event = [e for e in diag.events if e.data and "verdict" in e.data][0]
        assert "verdict_reminder" not in event.data
        assert "Verdict reminders" not in diag.to_text()


def _verdict_turn_with_search():
    return message([*search_blocks(), verdict_call(verdict_payload())])


def test_the_reminder_flag_is_never_cached():
    from src.verification.verification_cache import _SKIPPED_FIELDS

    assert "verdict_reminder_sent" in _SKIPPED_FIELDS


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------


def _silent_research_turn():
    return FakeMessage(
        content=[FakeTextBlock(text="I found the adopted code; recording it next.")],
        stop_reason="end_turn",
    )


class TestResearchReminder:
    def test_a_silent_turn_gets_one_reminder_and_the_items_count(self):
        logs: list[str] = []
        client = FakeResearchClient(
            _route_by_marker(
                {"ALPHA": [_silent_research_turn(), research_tool_use_response()]}
            )
        )
        profile = run_requirements_research(
            _enabled_module(),
            _complete_profile(),
            client=client,
            log=lambda message, **_kw: logs.append(message),
        )
        assert profile.completed_dimensions == 1
        assert profile.items
        first, reminded = client.calls
        assert [m["role"] for m in reminded["messages"]] == ["user", "assistant", "user"]
        assert reminded["messages"][2]["content"] == RESEARCH_SUBMIT_REMINDER
        assert reminded["system"] == first["system"]
        assert reminded["tools"] == first["tools"]
        assert any("after a reminder to submit" in line for line in logs)

    def test_only_one_reminder(self):
        client = FakeResearchClient(
            _route_by_marker(
                {"ALPHA": [_silent_research_turn(), _silent_research_turn()]}
            )
        )
        with pytest.raises(ResearchFanoutError, match="even after a reminder"):
            run_requirements_research(_enabled_module(), _complete_profile(), client=client)
        assert len(client.calls) == 2

    def test_a_client_tool_call_is_not_reminded(self):
        stray = FakeMessage(
            content=[FakeToolUseBlock(name="submit_findings", input={})],
            stop_reason="tool_use",
        )
        client = FakeResearchClient(_route_by_marker({"ALPHA": [stray]}))
        with pytest.raises(ResearchFanoutError, match="no parseable payload"):
            run_requirements_research(_enabled_module(), _complete_profile(), client=client)
        assert len(client.calls) == 1

    def test_the_reminder_is_in_the_diagnostics_event(self):
        diag = DiagnosticsReport()
        client = FakeResearchClient(
            _route_by_marker(
                {"ALPHA": [_silent_research_turn(), research_tool_use_response()]}
            )
        )
        run_requirements_research(
            _enabled_module(), _complete_profile(), client=client, diag=diag
        )
        events = [e for e in diag.events if e.data and e.data.get("dimension_id") == "alpha"]
        assert events and events[0].data.get("submission_reminder") is True


# ---------------------------------------------------------------------------
# The reminder is recorded on every exit (review of the first version)
# ---------------------------------------------------------------------------


class TestTheReminderIsNeverDropped:
    def test_research_failure_after_a_reminder(self):
        diag = DiagnosticsReport()
        cut = FakeMessage(content=[FakeTextBlock(text="Recording now…")], stop_reason="max_tokens")
        client = FakeResearchClient(_route_by_marker({"ALPHA": [_silent_research_turn(), cut]}))
        with pytest.raises(ResearchFanoutError, match="incomplete"):
            run_requirements_research(
                _enabled_module(), _complete_profile(), client=client, diag=diag
            )
        events = [e for e in diag.events if e.data and e.data.get("dimension_id") == "alpha"]
        assert events and events[0].data.get("submission_reminder") is True

    def test_research_retry_after_a_reminder(self, monkeypatch):
        import src.research.requirements_research as rr

        monkeypatch.setattr(rr.time, "sleep", lambda _s: None)
        diag = DiagnosticsReport()
        client = FakeResearchClient(
            _route_by_marker(
                {
                    "ALPHA": [
                        # Attempt 1: a silent turn, its reminder, then a
                        # retryable transport error.
                        _silent_research_turn(),
                        RuntimeError("connection reset by peer"),
                        # Attempt 2: answers at once.
                        research_tool_use_response(),
                    ]
                }
            )
        )
        run_requirements_research(_enabled_module(), _complete_profile(), client=client, diag=diag)
        events = [e for e in diag.events if e.data and e.data.get("dimension_id") == "alpha"]
        assert events[0].data.get("dimension_status") == "completed"
        assert events[0].data.get("submission_reminder") is True

    def test_realtime_retry_after_a_reminder(self, monkeypatch):
        import anthropic
        import httpx2

        dropped = anthropic.APIConnectionError(
            request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        result, client = run_realtime(
            monkeypatch,
            _sequence(_silent_turn(), dropped, _verdict_turn_with_search()),
            max_retries=1,
        )
        assert len(client.calls) == 3
        assert result.outcome == OUTCOME_VERDICT
        assert result.verdict_reminder_sent is True

    @pytest.mark.parametrize("reminded", ["initial", "escalation"])
    def test_an_escalation_merge_keeps_either_sides_reminder(self, reminded):
        initial = V.VerificationResult(verdict="UNVERIFIED", grounded=True)
        escalated = V.VerificationResult(
            verdict="CONFIRMED", grounded=True, accepted_sources=[SEARCHED_URL]
        )
        (initial if reminded == "initial" else escalated).verdict_reminder_sent = True
        merged = V._apply_escalation_outcome(
            initial_result=initial,
            esc_result=escalated,
            initial_verdict="UNVERIFIED",
            initial_model="claude-sonnet-5-5",
            initial_grounded=True,
            initial_sources=[],
            escalation_reason="initial_unverified",
        )
        assert merged is escalated
        assert merged.verdict_reminder_sent is True

    def test_a_batch_retry_after_a_reminder(self, monkeypatch):
        from types import SimpleNamespace

        from tests.fixtures.fake_anthropic import FakeBatchResultEnvelope

        # Wave 1: a silent turn -> its reminder. Wave 2: the reminder item
        # errors (retryable) -> wave 3 retries in a fresh conversation, which
        # answers. The reminder the abandoned conversation got stays recorded.
        def route(custom_id: str):
            if custom_id.startswith("verify_cont_1__"):
                return FakeBatchResultEnvelope(
                    type="errored",
                    error=SimpleNamespace(type="overloaded_error", message="Overloaded"),
                )
            if custom_id.startswith("verify_retry_"):
                return _verdict_turn_with_search()
            return _silent_turn()

        finding = run_batch(monkeypatch, route, max_waves=3)
        assert finding.verification.outcome == OUTCOME_VERDICT
        assert finding.verification.verdict_reminder_sent is True
