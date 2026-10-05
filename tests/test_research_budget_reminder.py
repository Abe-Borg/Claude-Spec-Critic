"""Budget recovery is a single submit-only turn in the original conversation."""
from __future__ import annotations

import copy

import pytest

from src.orchestration.diagnostics import DiagnosticsReport
from src.research import ResearchFanoutError, run_requirements_research
from src.research import requirements_research as rr
from tests.fixtures.fake_anthropic import (
    FakeContainer,
    FakeMessage,
    FakeServerToolUsage,
    FakeTextBlock,
    FakeThinkingBlock,
    pause_turn_response,
    research_tool_use_response,
)
from tests.fixtures.preserved_thinking import preserved_thinking_violations
from tests.test_requirements_research import (
    FakeResearchClient,
    _complete_profile,
    _dimension,
    _enabled_module,
)

URL = "https://codes.example.gov/adoption"


def _submit(**kwargs):
    return research_tool_use_response(
        payload={"items": [{
            "category": "governing_code",
            "requirement": "Apply the adopted code.",
            "source_urls": [URL],
            "confidence": 0.9,
        }]},
        searched_urls=[],
        **kwargs,
    )


def _silent():
    return FakeMessage(content=[FakeTextBlock(text="Still researching.")], stop_reason="end_turn")


def _fetch_pause(fetches, call_id):
    response = pause_turn_response(searched_urls=[URL], web_search_requests=0)
    response.usage.server_tool_use = FakeServerToolUsage(web_fetch_requests=fetches)
    response.content = [{
        "type": "server_tool_use", "id": call_id,
        "name": "web_fetch", "input": {"url": URL},
    }, {
        "type": "web_fetch_tool_result", "tool_use_id": call_id,
        "content": {"type": "web_fetch_result", "url": URL},
    }]
    return response


def _client(script):
    responses = iter(script)
    bodies = []
    exchanges = []

    def route(kwargs):
        body = copy.deepcopy(kwargs)
        bodies.append(body)
        response = next(responses)
        exchanges.append((body, None if isinstance(response, Exception) else response.content))
        return response

    return FakeResearchClient(route), bodies, exchanges


def _event(diag):
    return next(e.data for e in diag.events if e.data and e.data.get("dimension_id") == "alpha")


@pytest.fixture(params=["web_search", "web_fetch", "continuations"])
def budget(request, monkeypatch):
    reason = request.param
    dimension = _dimension(max_searches=1) if reason == "web_search" else _dimension(max_fetches=1)
    if reason == "web_fetch":
        # Every request respects max_uses=1; only the conversation total
        # exceeds its 2× ceiling on the third pause.
        pauses = [_fetch_pause(1, f"fetch_{index}") for index in range(3)]
        error = "Research exceeded the per-dimension web_fetch budget ceiling (3 > 2) without completing."
        searches, fetches = 0, 3
    else:
        if reason == "continuations":
            monkeypatch.setattr(rr, "RESEARCH_MAX_CONTINUATIONS", 1)
        pauses = [pause_turn_response(searched_urls=[URL], web_search_requests=1)
                  for _ in range(3 if reason == "web_search" else 2)]
        error = (
            "Research exceeded the per-dimension web_search budget ceiling (3 > 2) without completing."
            if reason == "web_search" else
            "Research did not complete after maximum continuation attempts (max_continuations=1)."
        )
        searches, fetches = (3 if reason == "web_search" else 2), 0
    for index, response in enumerate(pauses):
        response.content.insert(0, FakeThinkingBlock(signature=f"{reason}-{index}"))
    return _enabled_module(research_dimensions=(dimension,)), pauses, reason, error, searches, fetches


def test_budget_reminder_recovers_grounded_items(budget):
    module, pauses, reason, _error, searches, fetches = budget
    diag = DiagnosticsReport()
    client, bodies, exchanges = _client([*pauses, _submit()])
    profile = run_requirements_research(module, _complete_profile(), client=client, diag=diag)

    assert profile.completed_dimensions == 1
    assert len(profile.items) == 1 and profile.items[0].grounded
    assert profile.items[0].accepted_sources == [URL]
    status = profile.dimension_statuses[0]
    assert status.item_count == status.grounded_count == 1
    assert status.web_search_requests == searches
    assert status.web_fetch_requests == fetches
    assert len(bodies) == len(pauses) + 1
    # A normal resume is followed by the budget reminder in the same history.
    assert bodies[1]["messages"][-1]["role"] == "assistant"
    assert bodies[-1]["messages"][-1] == {
        "role": "user", "content": rr.RESEARCH_BUDGET_SUBMIT_REMINDER,
    }
    for body in bodies[1:]:
        assert body["system"] == bodies[0]["system"]
        assert body["tools"] == bodies[0]["tools"]
    assert preserved_thinking_violations(exchanges) == []
    event = _event(diag)
    assert event["submission_reminder"] is True
    assert event["budget_reminder"] is True
    assert event["budget_reminder_reason"] == reason
    assert event["budget_reminder_recovered"] is True


@pytest.mark.parametrize("final", ["silent", "pause", "paused_submit", "max_tokens", "search", "fetch", "search_attempt", "fetch_attempt", "error"])
def test_budget_reminder_failure_keeps_original_guard_message(budget, final):
    module, pauses, reason, error, _searches, _fetches = budget
    response = _silent()
    if final in {"pause", "max_tokens"}:
        response.stop_reason = "pause_turn" if final == "pause" else "max_tokens"
    elif final == "paused_submit":
        response = _submit(stop_reason="pause_turn")
    elif final in {"search", "fetch", "search_attempt", "fetch_attempt"}:
        response = _submit()
        if final == "search":
            response.usage.server_tool_use.web_search_requests = 1
        elif final == "fetch":
            response.usage.server_tool_use.web_fetch_requests = 1
        else:
            # Even an attempted call that bills no uses violates submit-only.
            response.content.insert(0, {
                "type": "server_tool_use", "id": "extra",
                "name": "web_search" if final == "search_attempt" else "web_fetch",
                "input": {},
            })
    elif final == "error":
        response = RuntimeError("boom")
    diag = DiagnosticsReport()
    client, bodies, _exchanges = _client([*pauses, response])
    with pytest.raises(ResearchFanoutError) as failure:
        run_requirements_research(module, _complete_profile(), client=client, diag=diag)
    assert error in str(failure.value)
    assert len(bodies) == len(pauses) + 1
    event = _event(diag)
    assert event["error"] == error
    assert event["item_count"] == 0
    assert event["budget_reminder"] is True
    assert event["budget_reminder_reason"] == reason
    assert event["budget_reminder_recovered"] is False


@pytest.mark.parametrize("finished", ["silent", "submit", "pause", "unresolved"])
def test_pending_container_calls_get_only_one_resume_before_reminder(finished):
    pause = pause_turn_response(searched_urls=[URL], web_search_requests=3)
    pause.content.insert(0, FakeThinkingBlock(signature="before-budget"))
    pause.content.extend([{
        "type": "server_tool_use", "id": "pending_code",
        "name": "code_execution", "input": {"code": "..."},
    }, {
        "type": "server_tool_use", "id": "pending_fetch",
        "name": "web_fetch", "input": {"url": URL},
        "caller": {"type": "code_execution_20260120", "tool_id": "pending_code"},
    }])
    pause.container = FakeContainer(id="container_budget")
    resumed = _submit() if finished == "submit" else _silent()
    if finished != "unresolved":
        resumed.content[0:0] = [{
            "type": "web_fetch_tool_result", "tool_use_id": "pending_fetch",
            "content": {"type": "web_fetch_result", "url": URL},
        }, {
            "type": "code_execution_tool_result", "tool_use_id": "pending_code",
            "content": {"type": "code_execution_result", "stdout": "done"},
        }]
    resumed.content.append(FakeThinkingBlock(signature="after-resume"))
    if finished == "pause":
        resumed.stop_reason = "pause_turn"
    diag = DiagnosticsReport()
    client, bodies, exchanges = _client([pause, resumed, _submit()])
    module = _enabled_module(research_dimensions=(_dimension(max_searches=1),))
    if finished in {"pause", "unresolved"}:
        with pytest.raises(ResearchFanoutError, match="web_search budget ceiling"):
            run_requirements_research(module, _complete_profile(), client=client, diag=diag)
    else:
        profile = run_requirements_research(module, _complete_profile(), client=client, diag=diag)
        assert profile.items[0].grounded

    resume = bodies[1]
    assert [m["role"] for m in resume["messages"]] == ["user", "assistant"]
    assert resume["messages"][-1]["content"] == pause.content
    assert resume["container"] == "container_budget"
    assert len(bodies) == (3 if finished == "silent" else 2)
    if finished == "silent":
        assert bodies[2]["container"] == "container_budget"
        assert bodies[2]["messages"][-1]["content"] == rr.RESEARCH_BUDGET_SUBMIT_REMINDER
        assert _event(diag)["budget_reminder_recovered"] is True
    else:
        assert "budget_reminder" not in _event(diag)
    assert preserved_thinking_violations(exchanges) == []


def test_exact_ceiling_can_resume_without_budget_reminder():
    client, bodies, _exchanges = _client([
        pause_turn_response(searched_urls=[URL], web_search_requests=2), _submit(),
    ])
    diag = DiagnosticsReport()
    run_requirements_research(
        _enabled_module(research_dimensions=(_dimension(max_searches=1),)),
        _complete_profile(), client=client, diag=diag,
    )
    assert len(bodies) == 2
    assert bodies[1]["messages"][-1]["role"] == "assistant"
    assert "budget_reminder" not in _event(diag)


def test_silent_completed_turn_over_budget_uses_submit_only_reminder():
    response = pause_turn_response(searched_urls=[URL], web_search_requests=3)
    response.stop_reason = "end_turn"
    client, bodies, _exchanges = _client([response, _submit()])
    run_requirements_research(
        _enabled_module(research_dimensions=(_dimension(max_searches=1),)),
        _complete_profile(), client=client,
    )
    assert len(bodies) == 2
    assert bodies[1]["messages"][-1]["content"] == rr.RESEARCH_BUDGET_SUBMIT_REMINDER


def test_budget_gets_final_reminder_after_the_ordinary_submit_reminder():
    client, bodies, _exchanges = _client([
        _silent(), pause_turn_response(searched_urls=[URL], web_search_requests=3), _submit(),
    ])
    run_requirements_research(
        _enabled_module(research_dimensions=(_dimension(max_searches=1),)),
        _complete_profile(), client=client,
    )
    assert len(bodies) == 3
    assert bodies[1]["messages"][-1]["content"] == rr.RESEARCH_SUBMIT_REMINDER
    assert bodies[2]["messages"][-1]["content"] == rr.RESEARCH_BUDGET_SUBMIT_REMINDER


def test_budget_reminder_retries_identical_request_without_resetting_budget(monkeypatch):
    monkeypatch.setattr(rr.time, "sleep", lambda _s: None)
    diag = DiagnosticsReport()
    client, bodies, exchanges = _client([
        pause_turn_response(searched_urls=[URL], web_search_requests=3),
        RuntimeError("connection reset by peer"),
        _submit(),
    ])
    profile = run_requirements_research(
        _enabled_module(research_dimensions=(_dimension(max_searches=1),)),
        _complete_profile(), client=client, diag=diag,
    )
    assert profile.items[0].grounded
    assert len(bodies) == 3 and bodies[1] == bodies[2]
    assert _event(diag)["budget_reminder_recovered"] is True
    assert preserved_thinking_violations(exchanges) == []


def test_invalid_budget_reminder_cannot_restart_research():
    from tests.test_conversation_retries import _error

    diag = DiagnosticsReport()
    client, bodies, _exchanges = _client([
        pause_turn_response(searched_urls=[URL], web_search_requests=3), _error(400), _submit(),
    ])
    with pytest.raises(ResearchFanoutError, match="web_search budget ceiling"):
        run_requirements_research(
            _enabled_module(research_dimensions=(_dimension(max_searches=1),)),
            _complete_profile(), client=client, diag=diag,
        )
    assert len(bodies) == 2
    event = _event(diag)
    assert event["budget_reminder_recovered"] is False
    assert "conversation_restarts" not in event
