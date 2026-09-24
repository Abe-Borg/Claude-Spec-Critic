"""Behavior of the HTML report's Ask AI chat, driven under Node (plan WP-12, chunk S04).

Each test writes a real report, runs its exact executable script in
``tests/fixtures/chat_harness.js`` against scripted Messages API streams, and
drives the chat like a reader. Nothing here re-implements the chat's parser:
what is checked is what the shipped script did — the request bodies it sent,
what it showed, and whether the controls came back.

The contract under test is the chat's transaction model (see the module
docstring of ``src/output/html_report_exporter.py`` and CLAUDE.md, "HTML report
+ Ask AI"): a question and every request it takes to answer it join the
conversation only when the model finishes its answer. Every other ending
leaves the conversation exactly as it was, and the proof is always the same:
the *next* request carries only what was committed. The plan's acceptance
cases each have a test here:

* an error event after a tool_use cannot corrupt the next turn —
  ``TestFailuresReachTheChat``;
* premature EOF, invalid tool JSON, request rejection, and reader failure all
  restore the UI and leave valid history — ``TestFailuresReachTheChat``;
* split frames and multibyte text reconstruct — ``TestStreamReconstruction``;
* citation deltas survive the next request — ``TestCitations``;
* Stop / New chat / model-change races cannot mutate the wrong conversation —
  ``TestStaleResponsesStayOut``;
* no API key is written to web storage — ``TestKeyLifetime`` (the exporter
  tests already pin that no key is ever written into the file).

Hermetic: the fetch is scripted and the key is fake. Node is a test-time tool;
locally a missing Node skips, and CI's ``SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS=1``
turns that into a failure.
"""
from __future__ import annotations

import hashlib
import base64
import json

import pytest

from tests.fixtures import chat_harness as h
from tests.fixtures.chat_harness import (
    FAKE_KEY,
    ask,
    choose,
    click,
    conversation,
    end_stream,
    ending,
    http_error,
    manual,
    message_start,
    network_error,
    open_chat,
    pagehide,
    push,
    reply,
    respond,
    save_key,
    settle,
    snapshot,
    start,
    delta,
    stop,
    text,
    thinking,
    tool_call,
    wait_idle,
    wait_requests,
    wait_text,
    whole,
)

Q1 = "Which findings matter most?"
Q2 = "What should we fix first?"
Q3 = "Anything else?"

DONE = reply(text("Done."))
MULTIBYTE = "Łódź — €42 ⚡ 日本語 🔥 ok"


@pytest.fixture(scope="module")
def chat(tmp_path_factory):
    h.node_or_skip()
    return h.ship_chat(tmp_path_factory.mktemp("shipped-chat"))


def run(chat, tmp_path, **kwargs) -> h.ChatRun:
    return h.run_chat(chat, tmp_path / "run", **kwargs)


def user(question: str) -> dict:
    return {"role": "user", "content": question}


def question_then(chat, tmp_path, first_response, *, extra_responses=(), extra_steps=()):
    """Ask Q1 (answered by ``first_response``), then Q2 (answered "Done.")."""
    return run(
        chat,
        tmp_path,
        responses=[first_response, *extra_responses, respond(DONE)],
        steps=[
            open_chat(),
            save_key(),
            ask(Q1),
            wait_idle(),
            snapshot("after-q1"),
            *extra_steps,
            ask(Q2),
            wait_idle(),
        ],
    )


def assert_rolled_back(result: h.ChatRun, *, request: int = 1) -> None:
    """The failed question left nothing behind: the next request is Q2 alone."""
    assert result.sent(request) == [user(Q2)]


# ---------------------------------------------------------------------------
# The harness runs what ships
# ---------------------------------------------------------------------------


class TestHarnessRunsTheShippedScript:
    def test_the_script_run_is_the_one_the_csp_hash_covers(self, chat):
        on_disk = chat.script_path.read_bytes()
        assert on_disk.decode("utf-8") == chat.script
        digest = "sha256-" + base64.b64encode(hashlib.sha256(on_disk).digest()).decode("ascii")
        assert digest == chat.csp_hash

    def test_the_page_description_has_every_chat_control(self, chat):
        page = json.loads(chat.page_path.read_text(encoding="utf-8"))
        ids = {element["id"] for element in page["elements"]}
        for element_id in (
            "sc-chat", "sc-chat-toggle", "sc-chat-messages", "sc-chat-input", "sc-chat-send",
            "sc-chat-stop", "sc-chat-new", "sc-chat-forget", "sc-chat-key", "sc-chat-keysave",
            "sc-chat-model", "sc-chat-effort", "sc-chat-status", "sc-chat-starters",
        ):
            assert element_id in ids, element_id
        stop_button = next(e for e in page["elements"] if e["id"] == "sc-chat-stop")
        assert stop_button["hidden"] is True

    def test_a_question_streams_into_an_answer(self, chat, tmp_path):
        result = run(
            chat,
            tmp_path,
            responses=[respond(reply(text("Here are ", "the top findings.")))],
            steps=conversation(Q1),
        )
        assert [m["text"] for m in result.answers()] == ["Here are the top findings."]
        assert result.errors() == [] and result.notices() == []
        result.assert_idle()
        (request,) = result.requests
        assert request["url"] == "https://api.anthropic.com/v1/messages"
        assert request["headers"]["x-api-key"] == FAKE_KEY
        assert request["headers"]["anthropic-version"] == "2023-06-01"
        assert request["headers"]["anthropic-dangerous-direct-browser-access"] == "true"
        body = request["body"]
        assert body["model"] == "claude-opus-5"
        assert body["stream"] is True
        assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert body["output_config"] == {"effort": "high"}
        assert body["messages"] == [user(Q1)]
        assert "container" not in body
        assert body["system"][1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        names = [tool["name"] for tool in body["tools"]]
        assert names[0] == "web_search" and "web_fetch" not in names  # Opus 5 has no web fetch
        assert "get_findings" in names

    def test_the_selected_model_and_effort_ride_the_request(self, chat, tmp_path):
        result = run(
            chat,
            tmp_path,
            responses=[respond(DONE)],
            steps=[
                open_chat(),
                save_key(),
                choose("sc-chat-model", "claude-sonnet-5"),
                choose("sc-chat-effort", "low"),
                ask(Q1),
                wait_idle(),
            ],
        )
        body = result.requests[0]["body"]
        assert body["model"] == "claude-sonnet-5"
        assert body["output_config"] == {"effort": "low"}
        assert "web_fetch" in [tool["name"] for tool in body["tools"]]


# ---------------------------------------------------------------------------
# Stream reconstruction
# ---------------------------------------------------------------------------


class TestStreamReconstruction:
    @pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
    def test_one_byte_reads_split_every_character_and_separator(self, chat, tmp_path, newline):
        answer = "Part one: " + MULTIBYTE + " end"
        result = question_then(
            chat,
            tmp_path,
            respond(reply(text("Part one: ", MULTIBYTE, " end")), newline=newline, chunk=1),
        )
        assert result.answers(result.snapshot("after-q1"))[0]["text"] == answer
        replayed = result.sent(1)[1]
        assert replayed == {"role": "assistant", "content": [{"type": "text", "text": answer}]}

    @pytest.mark.parametrize("size", [2, 3, 5, 7, 64])
    def test_other_read_sizes_reconstruct(self, chat, tmp_path, size):
        result = question_then(
            chat, tmp_path, respond(reply(text(MULTIBYTE, " / ", MULTIBYTE)), newline="\r\n", chunk=size)
        )
        assert result.sent(1)[1]["content"][0]["text"] == MULTIBYTE + " / " + MULTIBYTE

    def test_crlf_cut_between_its_cr_and_lf(self, chat, tmp_path):
        data = h.sse(reply(text("Line ", MULTIBYTE)), newline="\r\n")
        cuts = [i + 1 for i in range(len(data) - 1) if data[i : i + 2] == b"\r\n"]
        assert len(cuts) > 10
        result = question_then(chat, tmp_path, respond(raw=data, cuts=cuts))
        assert result.sent(1)[1]["content"][0]["text"] == "Line " + MULTIBYTE

    @pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
    def test_multi_line_data_frames(self, chat, tmp_path, newline):
        # One-byte reads put a chunk boundary after every CR, including those
        # inside a frame's run of data lines; treating that CR as a finished
        # line before its LF arrives would dispatch half a JSON payload.
        result = question_then(
            chat, tmp_path, respond(reply(text("Spread ", MULTIBYTE)), multiline=True, newline=newline, chunk=1)
        )
        assert result.sent(1)[1]["content"][0]["text"] == "Spread " + MULTIBYTE

    def test_comments_pings_and_unknown_events_are_ignored(self, chat, tmp_path):
        events = reply(text("Still ", "fine."))
        data = (
            b": opening comment\n\n"
            + h.sse([{"type": "some_future_event", "detail": 1}])
            + h.sse(events[:2])
            + b"id: 7\nretry: 1000\n\n"
            + h.sse([{"type": "ping"}, delta(0, {"type": "some_future_delta", "value": "x"})])
            + h.sse(events[2:])
        )
        result = question_then(chat, tmp_path, respond(raw=data, chunk=4))
        assert result.errors(result.snapshot("after-q1")) == []
        assert result.sent(1)[1]["content"] == [{"type": "text", "text": "Still fine."}]

    def test_text_carried_by_a_start_event_is_shown_and_kept(self, chat, tmp_path):
        events = [
            message_start(),
            start(0, {"type": "text", "text": "Opening words, "}),
            delta(0, {"type": "text_delta", "text": "then the rest."}),
            stop(0),
            *ending("end_turn"),
        ]
        result = question_then(chat, tmp_path, respond(events))
        assert result.answers(result.snapshot("after-q1"))[0]["text"] == "Opening words, then the rest."
        assert result.sent(1)[1]["content"] == [{"type": "text", "text": "Opening words, then the rest."}]

    def test_a_character_cut_off_at_the_end_of_the_stream_is_incomplete(self, chat, tmp_path):
        opening = h.sse([message_start(), start(0, {"type": "text", "text": ""})])
        cut_frame = h.sse([delta(0, {"type": "text_delta", "text": "Cut at 🔥"})])[:-4]
        result = question_then(chat, tmp_path, respond(raw=opening + cut_frame, chunk=5))
        after = result.snapshot("after-q1")
        assert any("ended before it was complete" in e for e in result.errors(after))
        result.assert_idle(after)
        assert_rolled_back(result)


# ---------------------------------------------------------------------------
# Failures reach the chat, and the history stays valid
# ---------------------------------------------------------------------------

NAVIGATE = "navigate_to_section"


def _navigate(tool_id: str, *pieces: str):
    return tool_call(tool_id, NAVIGATE, *pieces)


class TestFailuresReachTheChat:
    def test_an_error_event_after_a_tool_call_cannot_corrupt_the_next_turn(self, chat, tmp_path):
        """The review's P1-5 case: the error used to be swallowed by the JSON
        guard, the half-built tool_use went into history with input {}, and
        every later request was rejected for a tool_use without a result."""
        events = [
            message_start(),
            *text("Let me check. ")(0),
            start(1, {"type": "tool_use", "id": "toolu_1", "name": NAVIGATE, "input": {}}),
            delta(1, {"type": "input_json_delta", "partial_json": '{"target_id": "sc-sum'}),
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        ]
        result = question_then(chat, tmp_path, respond(events))
        after = result.snapshot("after-q1")
        assert any("overloaded" in e for e in result.errors(after))
        assert result.effects == []  # the half-received call never ran
        result.assert_idle(after)
        assert after["input_value"] == Q1  # the question is back in the box
        assert_rolled_back(result)

    def test_an_error_event_in_a_later_round_discards_the_whole_turn(self, chat, tmp_path):
        first = reply(_navigate("toolu_1", '{"target_id": "sc-summary"}'), stop_reason="tool_use")
        second = [message_start(), *text("Found it")(0)[:2],
                  {"type": "error", "error": {"type": "api_error", "message": "Internal server error"}}]
        result = question_then(chat, tmp_path, respond(first), extra_responses=[respond(second)])
        after = result.snapshot("after-q1")
        assert result.effects == [{"effect": "scrollIntoView", "id": "sc-summary"}]
        assert any("Internal server error" in e and "api_error" in e for e in result.errors(after))
        assert_rolled_back(result, request=2)

    def test_premature_eof_is_not_success(self, chat, tmp_path):
        events = [message_start(), start(0, {"type": "text", "text": ""}),
                  delta(0, {"type": "text_delta", "text": "Half an ans"})]
        result = question_then(chat, tmp_path, respond(events))
        after = result.snapshot("after-q1")
        assert any("ended before it was complete" in e for e in result.errors(after))
        (answer,) = result.answers(after)
        assert "Half an ans" in answer["text"]
        assert "sc-msg-interrupted" in answer["classes"]
        assert "not added to the conversation" in answer["text"]
        result.assert_idle(after)
        assert_rolled_back(result)

    def test_eof_after_the_stop_reason_but_before_message_stop(self, chat, tmp_path):
        result = question_then(chat, tmp_path, respond(reply(text("Almost."), message_stop=False)))
        assert any("ended before it was complete" in e for e in result.errors(result.snapshot("after-q1")))
        assert_rolled_back(result)

    def test_a_final_event_without_its_blank_line_is_not_dispatched(self, chat, tmp_path):
        data = h.sse(reply(text("Complete-looking.")))
        assert data.endswith(b"\n\n")
        result = question_then(chat, tmp_path, respond(raw=data[:-1]))
        assert any("ended before it was complete" in e for e in result.errors(result.snapshot("after-q1")))
        assert_rolled_back(result)

    @pytest.mark.parametrize(
        "status,fragment",
        [
            (400, "Bad request"),
            (403, "permission"),
            (404, "not found"),
            (413, "too large"),
            (429, "Rate limited"),
            (500, "server error"),
            (529, "overloaded"),
        ],
    )
    def test_a_rejected_request_restores_the_ui_and_history(self, chat, tmp_path, status, fragment):
        result = question_then(chat, tmp_path, http_error(status))
        after = result.snapshot("after-q1")
        assert any(fragment in e for e in result.errors(after)), result.errors(after)
        assert result.answers(after) == []  # an empty answer bubble is removed
        result.assert_idle(after)
        assert_rolled_back(result)

    def test_a_rejected_key_is_forgotten(self, chat, tmp_path):
        result = run(
            chat,
            tmp_path,
            responses=[http_error(401, error_type="authentication_error", message="invalid x-api-key")],
            steps=[*conversation(Q1), ask(Q2), settle()],
        )
        assert result.final["ready"] is False
        assert "not accepted" in result.final["key_message"]
        assert len(result.requests) == 1  # no key, no request
        result.assert_idle()

    def test_a_transport_failure(self, chat, tmp_path):
        result = question_then(chat, tmp_path, network_error())
        after = result.snapshot("after-q1")
        assert any("Could not reach the Anthropic API" in e for e in result.errors(after))
        result.assert_idle(after)
        assert_rolled_back(result)

    def test_a_reader_failure_mid_stream(self, chat, tmp_path):
        events = [message_start(), start(0, {"type": "text", "text": ""}),
                  delta(0, {"type": "text_delta", "text": "Streaming along"})]
        result = question_then(chat, tmp_path, respond(events, read_error="connection reset"))
        after = result.snapshot("after-q1")
        assert any("connection dropped" in e for e in result.errors(after))
        assert "Streaming along" in result.answers(after)[0]["text"]
        result.assert_idle(after)
        assert_rolled_back(result)

    def test_a_rejection_in_a_later_tool_round_discards_the_turn(self, chat, tmp_path):
        first = reply(tool_call("toolu_1", "get_findings", '{"severity": "HIGH"}'), stop_reason="tool_use")
        result = question_then(chat, tmp_path, respond(first), extra_responses=[http_error(500)])
        assert any("server error" in e for e in result.errors(result.snapshot("after-q1")))
        assert_rolled_back(result, request=2)

    MALFORMED = {
        "not_json": lambda: h.sse([message_start()]) + b"data: {not json}\n\n",
        "before_message_start": lambda: h.sse([start(0, {"type": "text", "text": ""})]),
        "second_message_start": lambda: h.sse([message_start(), message_start()]),
        "block_not_open": lambda: h.sse([message_start(), delta(3, {"type": "text_delta", "text": "x"})]),
        "block_out_of_order": lambda: h.sse([message_start(), start(1, {"type": "text", "text": ""})]),
        "text_in_a_tool_block": lambda: h.sse(
            [message_start(), start(0, {"type": "tool_use", "id": "toolu_1", "name": "calculate", "input": {}}),
             delta(0, {"type": "text_delta", "text": "x"})]
        ),
        "citation_outside_text": lambda: h.sse(
            [message_start(), start(0, {"type": "thinking", "thinking": ""}),
             delta(0, {"type": "citations_delta", "citation": {"type": "web_search_result_location"}})]
        ),
        "never_finished": lambda: h.sse(
            [message_start(), start(0, {"type": "text", "text": ""}), *ending("end_turn")]
        ),
        "no_stop_reason": lambda: h.sse(reply(text("x"), stop_reason=None)),
        "event_after_message_stop": lambda: h.sse(reply(text("x")) + [start(1, {"type": "text", "text": ""})]),
        "not_utf8": lambda: h.sse([message_start()]) + b'data: {"type": "ping", "x": "\xff\xfe"}\n\n',
        "unsigned_thinking": lambda: h.sse(
            [message_start(), start(0, {"type": "thinking", "thinking": ""}),
             delta(0, {"type": "thinking_delta", "thinking": "hmm"}), stop(0),
             *text("Answer.")(1), *ending("end_turn")]
        ),
    }

    @pytest.mark.parametrize("case", sorted(MALFORMED))
    def test_a_malformed_stream_is_an_error_not_an_answer(self, chat, tmp_path, case):
        result = question_then(chat, tmp_path, respond(raw=self.MALFORMED[case]()))
        after = result.snapshot("after-q1")
        assert any("malformed" in e for e in result.errors(after)), result.errors(after)
        result.assert_idle(after)
        assert_rolled_back(result)

    @pytest.mark.parametrize(
        "pieces",
        [
            ('{"target_id": "sc-sum',),
            ('{"target_id": "sc-summary"}', "}"),
            ('["sc-summary"]',),
            ('"sc-summary"',),
            ("null",),
        ],
        ids=["truncated", "trailing_garbage", "array", "string", "null"],
    )
    def test_invalid_tool_input_is_neither_run_nor_replayed(self, chat, tmp_path, pieces):
        response = reply(text("Opening it."), _navigate("toolu_1", *pieces), stop_reason="tool_use")
        result = question_then(chat, tmp_path, respond(response))
        after = result.snapshot("after-q1")
        assert result.effects == []
        assert after["request_count"] == 1  # no tool_result was ever sent
        assert any("malformed" in e and "tool call" in e for e in result.errors(after))
        assert_rolled_back(result)

    def test_a_call_missing_a_required_field_gets_an_error_result_not_defaults(self, chat, tmp_path):
        first = reply(_navigate("toolu_1", "{}"), stop_reason="tool_use")
        result = run(
            chat,
            tmp_path,
            responses=[respond(first), respond(reply(text("I need a section id.")))],
            steps=conversation(Q1),
        )
        assert result.effects == []
        tool_result = result.sent(1)[-1]["content"][0]
        assert tool_result["tool_use_id"] == "toolu_1"
        assert tool_result["is_error"] is True
        assert "missing required input target_id" in tool_result["content"]
        assert result.errors() == []


# ---------------------------------------------------------------------------
# Stop reasons and limits
# ---------------------------------------------------------------------------

SEARCH_CALL = tool_call("srvtoolu_1", "web_search", '{"query": "NFPA 13 hanger spacing"}', kind="server_tool_use")
SEARCH_RESULT = {
    "type": "web_search_tool_result",
    "tool_use_id": "srvtoolu_1",
    "content": [
        {
            "type": "web_search_result",
            "url": "https://codes.example.org/nfpa13",
            "title": "NFPA 13 (2025)",
            "encrypted_content": "EncryptedContentA==",
            "page_age": None,
        }
    ],
}


class TestStopReasons:
    def test_max_tokens_is_shown_as_cut_off_and_never_replayed(self, chat, tmp_path):
        result = question_then(chat, tmp_path, respond(reply(text("A long answer that"), stop_reason="max_tokens")))
        after = result.snapshot("after-q1")
        assert any("length limit" in n for n in result.notices(after))
        (answer,) = result.answers(after)
        assert "A long answer that" in answer["text"] and "not added to the conversation" in answer["text"]
        assert_rolled_back(result)

    def test_max_tokens_inside_a_tool_call_runs_nothing(self, chat, tmp_path):
        response = reply(text("Let me"), _navigate("toolu_1", '{"target_id": "sc-'), stop_reason="max_tokens")
        result = question_then(chat, tmp_path, respond(response))
        assert result.effects == []
        assert result.snapshot("after-q1")["request_count"] == 1
        assert_rolled_back(result)

    def test_a_refusal_is_reported_and_discarded(self, chat, tmp_path):
        details = {"type": "refusal", "category": "cyber", "explanation": None}
        result = question_then(
            chat, tmp_path, respond(reply(text("I can"), stop_reason="refusal", stop_details=details))
        )
        notices = result.notices(result.snapshot("after-q1"))
        assert any("declined" in n and "(cyber)" in n for n in notices), notices
        assert_rolled_back(result)

    def test_the_context_window_limit(self, chat, tmp_path):
        result = question_then(
            chat, tmp_path, respond(reply(text("…"), stop_reason="model_context_window_exceeded"))
        )
        assert any("context window" in n for n in result.notices(result.snapshot("after-q1")))
        assert_rolled_back(result)

    def test_an_unknown_stop_reason_is_not_success(self, chat, tmp_path):
        result = question_then(chat, tmp_path, respond(reply(text("?"), stop_reason="brand_new_reason")))
        assert any("unexpected reason (brand_new_reason)" in e for e in result.errors(result.snapshot("after-q1")))
        assert_rolled_back(result)

    def test_a_stop_sequence_commits_like_end_turn(self, chat, tmp_path):
        result = question_then(chat, tmp_path, respond(reply(text("Stopped cleanly."), stop_reason="stop_sequence")))
        assert result.sent(1) == [
            user(Q1),
            {"role": "assistant", "content": [{"type": "text", "text": "Stopped cleanly."}]},
            user(Q2),
        ]

    def test_pause_turn_continues_with_the_paused_content_and_its_container(self, chat, tmp_path):
        container = {"id": "container_abc", "expires_at": "2026-10-01T00:00:00Z"}
        paused = reply(text("Searching. "), SEARCH_CALL, stop_reason="pause_turn", container=container)
        resumed = reply(whole(SEARCH_RESULT), text("NFPA 13 sets the spacing."))
        result = question_then(chat, tmp_path, respond(paused), extra_responses=[respond(resumed)])
        paused_content = [
            {"type": "text", "text": "Searching. "},
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
             "input": {"query": "NFPA 13 hanger spacing"}},
        ]
        assert "container" not in result.requests[0]["body"]
        assert result.requests[1]["body"]["container"] == "container_abc"
        assert result.sent(1) == [user(Q1), {"role": "assistant", "content": paused_content}]
        resumed_content = [SEARCH_RESULT, {"type": "text", "text": "NFPA 13 sets the spacing."}]
        assert result.sent(2) == [
            user(Q1),
            {"role": "assistant", "content": paused_content},
            {"role": "assistant", "content": resumed_content},
            user(Q2),
        ]
        assert "container" not in result.requests[2]["body"]  # a new turn starts fresh

    def test_the_continuation_limit_stops_visibly_and_discards(self, chat, tmp_path):
        pauses = [respond(reply(text(f"Working {i}. "), stop_reason="pause_turn")) for i in range(6)]
        result = question_then(chat, tmp_path, pauses[0], extra_responses=pauses[1:])
        after = result.snapshot("after-q1")
        assert after["request_count"] == 6
        assert any("paused 5 times" in n for n in result.notices(after))
        assert_rolled_back(result, request=6)

    def test_the_tool_round_limit_stops_visibly_and_discards(self, chat, tmp_path):
        rounds = [
            respond(reply(_navigate(f"toolu_{i}", '{"target_id": "sc-summary"}'), stop_reason="tool_use"))
            for i in range(9)
        ]
        result = question_then(chat, tmp_path, rounds[0], extra_responses=rounds[1:])
        after = result.snapshot("after-q1")
        assert after["request_count"] == 9
        assert len(result.effects) == 8  # eight rounds ran; the ninth call did not
        assert any("used report tools 8 times" in n for n in result.notices(after))
        assert_rolled_back(result, request=9)

    def test_a_tool_use_stop_without_a_report_tool_call_is_malformed(self, chat, tmp_path):
        result = question_then(chat, tmp_path, respond(reply(text("x"), SEARCH_CALL, stop_reason="tool_use")))
        assert any("malformed" in e for e in result.errors(result.snapshot("after-q1")))
        assert_rolled_back(result)

    def test_an_empty_answer_is_not_committed(self, chat, tmp_path):
        result = question_then(chat, tmp_path, respond(reply()))
        after = result.snapshot("after-q1")
        assert any("without answering" in n for n in result.notices(after))
        assert result.answers(after) == []
        assert_rolled_back(result)

    def test_a_turn_that_ends_right_after_its_tool_results_keeps_the_exchange(self, chat, tmp_path):
        first = reply(tool_call("toolu_1", "clear_filters", "{}"), stop_reason="tool_use")
        result = question_then(chat, tmp_path, respond(first), extra_responses=[respond(reply())])
        assert result.errors(result.snapshot("after-q1")) == []
        sent = result.sent(2)
        assert sent[0] == user(Q1)
        assert sent[1]["content"] == [{"type": "tool_use", "id": "toolu_1", "name": "clear_filters", "input": {}}]
        assert sent[2]["content"][0]["tool_use_id"] == "toolu_1"
        assert sent[3] == user(Q2)


# ---------------------------------------------------------------------------
# The conversation transaction
# ---------------------------------------------------------------------------


class TestConversationTransaction:
    def test_a_completed_tool_turn_commits_every_pair_in_order(self, chat, tmp_path):
        first = reply(
            thinking("Look it up.", signature="sig-1"),
            text("Checking. "),
            tool_call("toolu_1", "get_findings", '{"severity":', ' "HIGH"}'),
            stop_reason="tool_use",
        )
        second = reply(text("There are HIGH findings."))
        result = question_then(chat, tmp_path, respond(first), extra_responses=[respond(second)])
        assistant = {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Look it up.", "signature": "sig-1"},
                {"type": "text", "text": "Checking. "},
                {"type": "tool_use", "id": "toolu_1", "name": "get_findings", "input": {"severity": "HIGH"}},
            ],
        }
        sent = result.sent(1)
        assert sent[:2] == [user(Q1), assistant]
        (tool_result,) = sent[2]["content"]
        assert sent[2]["role"] == "user" and tool_result["tool_use_id"] == "toolu_1"
        assert "is_error" not in tool_result
        assert json.loads(tool_result["content"])["total_matching"] >= 1
        assert result.sent(2) == [
            *sent,
            {"role": "assistant", "content": [{"type": "text", "text": "There are HIGH findings."}]},
            user(Q2),
        ]

    def test_web_tool_blocks_replay_exactly_as_received(self, chat, tmp_path):
        response = reply(text("Searching. "), SEARCH_CALL, whole(SEARCH_RESULT), text("It is 12 ft."))
        result = question_then(chat, tmp_path, respond(response))
        assert result.sent(1)[1]["content"] == [
            {"type": "text", "text": "Searching. "},
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
             "input": {"query": "NFPA 13 hanger spacing"}},
            SEARCH_RESULT,
            {"type": "text", "text": "It is 12 ft."},
        ]

    def test_an_unresolved_web_tool_call_is_never_committed(self, chat, tmp_path):
        result = question_then(chat, tmp_path, respond(reply(text("Looking. "), SEARCH_CALL, text("Done."))))
        assert any("web tool call without its result" in e for e in result.errors(result.snapshot("after-q1")))
        assert_rolled_back(result)

    def test_the_interrupted_exchange_is_marked_and_the_question_restored(self, chat, tmp_path):
        events = [message_start(), start(0, {"type": "text", "text": ""}),
                  delta(0, {"type": "text_delta", "text": "Partial"})]
        result = question_then(chat, tmp_path, respond(events))
        after = result.snapshot("after-q1")
        question_bubble, answer = result.of_kind("user", after)[0], result.answers(after)[0]
        assert "sc-msg-interrupted" in question_bubble["classes"]
        assert "sc-msg-interrupted" in answer["classes"]
        assert answer["text"].endswith("Interrupted — this answer was not added to the conversation.")
        assert after["input_value"] == Q1
        assert any("question is back in the message box" in e for e in result.errors(after))

    def test_a_failed_turn_leaves_earlier_turns_intact(self, chat, tmp_path):
        result = run(
            chat,
            tmp_path,
            responses=[respond(reply(text("First answer."))), http_error(500), respond(DONE)],
            steps=conversation(Q1, Q2, Q3),
        )
        assert result.sent(2) == [
            user(Q1),
            {"role": "assistant", "content": [{"type": "text", "text": "First answer."}]},
            user(Q3),
        ]

    def test_history_is_trimmed_by_whole_turns(self, chat, tmp_path):
        tool_round = [
            respond(reply(tool_call(f"toolu_{i}", "get_findings", "{}"), stop_reason="tool_use")) for i in range(3)
        ]
        questions = [f"Question {n}" for n in range(1, 12)]
        responses = [*tool_round, respond(reply(text("Answer 1.")))]
        responses += [respond(reply(text(f"Answer {n}."))) for n in range(2, 12)]
        result = run(chat, tmp_path, responses=responses, steps=conversation(*questions))
        # Turn 1 is 8 messages; turns 2-9 bring the history to 24. Committing
        # turn 10 would make 26, so turn 1 leaves whole.
        tenth = result.sent(12)
        assert len(tenth) == 25 and tenth[0] == user("Question 1")
        eleventh = result.sent(13)
        assert eleventh[0] == user("Question 2")
        assert len(eleventh) == 19
        assert not any(
            isinstance(m["content"], list) and any(b.get("type") in ("tool_use", "tool_result") for b in m["content"])
            for m in eleventh
        )


# ---------------------------------------------------------------------------
# Stop, New chat, and a model change
# ---------------------------------------------------------------------------

PARTIAL = [message_start(), start(0, {"type": "text", "text": ""}), delta(0, {"type": "text_delta", "text": "Partial"})]
LATE = [delta(0, {"type": "text_delta", "text": " LATE TEXT"}), stop(0), *ending("end_turn")]


class TestStaleResponsesStayOut:
    def test_stop_restores_the_controls_at_once_and_late_chunks_are_ignored(self, chat, tmp_path):
        result = run(
            chat,
            tmp_path,
            responses=[manual("r1", ignore_abort=True), respond(DONE)],
            steps=[
                open_chat(), save_key(), ask(Q1), wait_requests(1),
                push("r1", PARTIAL), wait_text("Partial"),
                click("sc-chat-stop"), snapshot("stopped"),
                push("r1", LATE), end_stream("r1"), settle(), snapshot("after-late"),
                ask(Q2), wait_idle(),
            ],
        )
        stopped = result.snapshot("stopped")
        result.assert_idle(stopped)
        assert any(n.startswith("Stopped.") for n in result.notices(stopped))
        assert stopped["input_value"] == Q1
        late = result.snapshot("after-late")
        assert all("LATE TEXT" not in m["text"] for m in late["messages"])
        assert late["messages"] == stopped["messages"]
        assert result.streams[0]["cancelled"] is True  # the abandoned stream was released
        assert result.sent(1) == [user(Q2)]

    def test_new_chat_mid_answer_keeps_the_old_answer_out(self, chat, tmp_path):
        result = run(
            chat,
            tmp_path,
            responses=[manual("r1", ignore_abort=True), respond(reply(text("Fresh answer.")))],
            steps=[
                open_chat(), save_key(), ask(Q1), wait_requests(1),
                push("r1", PARTIAL), wait_text("Partial"),
                click("sc-chat-new"), snapshot("cleared"),
                push("r1", LATE), end_stream("r1"), settle(), snapshot("after-late"),
                ask(Q2), wait_idle(),
            ],
        )
        cleared = result.snapshot("cleared")
        assert cleared["messages"] == []
        assert cleared["starters_hidden"] is False
        assert cleared["input_value"] == ""  # a new chat does not refill the box
        result.assert_idle(cleared)
        assert result.snapshot("after-late")["messages"] == []
        assert result.sent(1) == [user(Q2)]
        assert [m["text"] for m in result.final["messages"]] == [Q2, "Fresh answer."]

    def test_new_chat_during_a_tool_round(self, chat, tmp_path):
        first = reply(tool_call("toolu_1", "get_findings", "{}"), stop_reason="tool_use")
        result = run(
            chat,
            tmp_path,
            responses=[respond(first), manual("r2", ignore_abort=True), respond(DONE)],
            steps=[
                open_chat(), save_key(), ask(Q1), wait_requests(2),
                click("sc-chat-new"),
                push("r2", reply(text("A late answer."))), end_stream("r2"), settle(), snapshot("after-late"),
                ask(Q2), wait_idle(),
            ],
        )
        assert result.snapshot("after-late")["messages"] == []
        assert result.sent(2) == [user(Q2)]

    def test_a_model_change_stops_the_answer_and_the_next_turn_uses_the_new_model(self, chat, tmp_path):
        result = run(
            chat,
            tmp_path,
            responses=[manual("r1", ignore_abort=True), respond(DONE)],
            steps=[
                open_chat(), save_key(), ask(Q1), wait_requests(1),
                push("r1", PARTIAL), wait_text("Partial"),
                choose("sc-chat-model", "claude-sonnet-5"), snapshot("switched"),
                push("r1", LATE), end_stream("r1"), settle(),
                ask(Q2), wait_idle(),
            ],
        )
        switched = result.snapshot("switched")
        result.assert_idle(switched)
        assert any("model was changed" in n for n in result.notices(switched))
        assert result.requests[0]["body"]["model"] == "claude-opus-5"
        second = result.requests[1]["body"]
        assert second["model"] == "claude-sonnet-5"
        assert "web_fetch" in [tool["name"] for tool in second["tools"]]
        assert second["messages"] == [user(Q2)]

    def test_a_stopped_turn_cannot_disturb_the_turn_after_it(self, chat, tmp_path):
        result = run(
            chat,
            tmp_path,
            responses=[manual("r1", ignore_abort=True), manual("r2"), respond(DONE)],
            steps=[
                open_chat(), save_key(), ask(Q1), wait_requests(1),
                push("r1", PARTIAL), wait_text("Partial"), click("sc-chat-stop"),
                ask(Q2), wait_requests(2),
                push("r1", LATE), end_stream("r1"), settle(), snapshot("second-running"),
                push("r2", reply(text("Second answer."))), end_stream("r2"), wait_idle(),
                ask(Q3), wait_idle(),
            ],
        )
        running = result.snapshot("second-running")
        assert running["send_disabled"] is True and running["stop_hidden"] is False
        assert all("LATE TEXT" not in m["text"] for m in running["messages"])
        assert result.sent(2) == [
            user(Q2),
            {"role": "assistant", "content": [{"type": "text", "text": "Second answer."}]},
            user(Q3),
        ]

    def test_forget_key_mid_answer_stops_it(self, chat, tmp_path):
        result = run(
            chat,
            tmp_path,
            responses=[manual("r1")],
            steps=[
                open_chat(), save_key(), ask(Q1), wait_requests(1),
                push("r1", PARTIAL), wait_text("Partial"),
                click("sc-chat-forget"), settle(),
            ],
        )
        assert any("key was forgotten" in n for n in result.notices())
        assert result.final["ready"] is False
        result.assert_idle()
        assert result.streams[0]["aborted"] is True

    def test_leaving_the_page_forgets_the_key_and_abandons_the_answer(self, chat, tmp_path):
        result = run(
            chat,
            tmp_path,
            responses=[manual("r1")],
            steps=[
                open_chat(), save_key(), ask(Q1), wait_requests(1),
                push("r1", PARTIAL), wait_text("Partial"),
                pagehide(), settle(),
            ],
        )
        assert result.final["ready"] is False
        assert result.final["status"] == ""
        assert result.streams[0]["aborted"] is True
        result.assert_idle()


# ---------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------

CITE_A = {
    "type": "web_search_result_location",
    "url": "https://codes.example.org/nfpa13",
    "title": "NFPA 13 (2025) 17.4",
    "encrypted_index": "EncryptedIndexA==",
    "cited_text": "Hangers shall be spaced...",
}
CITE_B = {
    "type": "web_search_result_location",
    "url": "https://codes.example.org/nfpa72",
    "title": "NFPA 72 (2025)",
    "encrypted_index": "EncryptedIndexB==",
    "cited_text": "Alarms shall...",
}
CITE_WITHOUT_URL = {
    "type": "char_location",
    "cited_text": "an excerpt",
    "document_index": 0,
    "document_title": "Some document",
    "start_char_index": 0,
    "end_char_index": 10,
}
CITE_PLAIN_HTTP = {
    "type": "web_search_result_location",
    "url": "http://insecure.example.org/page",
    "title": "Insecure page",
    "encrypted_index": "EncryptedIndexC==",
    "cited_text": "plain http",
}


class TestCitations:
    def test_citations_stay_with_their_text_block_and_survive_the_next_request(self, chat, tmp_path):
        response = reply(
            text("Let me search. "),
            SEARCH_CALL,
            whole(SEARCH_RESULT),
            text("Per the standard, "),
            text("hangers are required", citations=[CITE_A, CITE_B]),
            text(" and alarms are too", citations=[CITE_A], citations_first=False),
            text("."),
        )
        result = question_then(chat, tmp_path, respond(response, chunk=11))
        replayed = result.sent(1)[1]["content"]
        assert replayed[3] == {"type": "text", "text": "Per the standard, "}
        assert replayed[4] == {"type": "text", "text": "hangers are required", "citations": [CITE_A, CITE_B]}
        assert replayed[5] == {"type": "text", "text": " and alarms are too", "citations": [CITE_A]}
        assert replayed[6] == {"type": "text", "text": "."}
        assert replayed[2] == SEARCH_RESULT
        (answer,) = result.answers(result.snapshot("after-q1"))
        assert "hangers are required [1][2] and alarms are too [1]." in answer["text"]
        assert answer["links"] == [
            {"href": CITE_A["url"], "text": "[1] " + CITE_A["title"]},
            {"href": CITE_B["url"], "text": "[2] " + CITE_B["title"]},
        ]

    def test_a_citation_without_an_https_url_is_replayed_but_never_shown(self, chat, tmp_path):
        response = reply(text("A claim", citations=[CITE_WITHOUT_URL, CITE_PLAIN_HTTP]))
        result = question_then(chat, tmp_path, respond(response))
        assert result.sent(1)[1]["content"] == [
            {"type": "text", "text": "A claim", "citations": [CITE_WITHOUT_URL, CITE_PLAIN_HTTP]}
        ]
        (answer,) = result.answers(result.snapshot("after-q1"))
        assert answer["text"] == "A claim"
        assert answer["links"] == []

    def test_citations_on_an_interrupted_answer_still_name_their_sources(self, chat, tmp_path):
        events = [message_start(), *text("Cited text", citations=[CITE_A])(0)]
        result = question_then(chat, tmp_path, respond(events))
        (answer,) = result.answers(result.snapshot("after-q1"))
        assert "Cited text [1]" in answer["text"]
        assert answer["links"] == [{"href": CITE_A["url"], "text": "[1] " + CITE_A["title"]}]
        assert_rolled_back(result)


# ---------------------------------------------------------------------------
# The API key
# ---------------------------------------------------------------------------


def _mentions_key(result: h.ChatRun, key: str = FAKE_KEY) -> bool:
    written = [entry.get("value", "") for entry in result.storage_log]
    stored = [*result.storage["sessionStorage"].values(), *result.storage["localStorage"].values()]
    return any(key in value for value in written + stored)


class TestKeyLifetime:
    def test_the_key_is_used_but_never_written_to_web_storage(self, chat, tmp_path):
        result = run(chat, tmp_path, responses=[respond(DONE)], steps=conversation(Q1))
        assert result.requests[0]["headers"]["x-api-key"] == FAKE_KEY
        assert not _mentions_key(result)
        assert result.storage_log == [{"storage": "sessionStorage", "op": "remove", "key": "sc_api_key"}]

    def test_a_key_left_by_an_older_report_is_removed_and_never_reused(self, chat, tmp_path):
        legacy = "sk-ant-api03-LEGACY-left-by-an-older-report"
        result = run(
            chat,
            tmp_path,
            preload={"sessionStorage": {"sc_api_key": legacy, "sc_chat_effort": "low"}},
            steps=[open_chat(), snapshot("opened"), ask(Q1), settle()],
        )
        assert result.snapshot("opened")["ready"] is False
        assert "sc_api_key" not in result.storage["sessionStorage"]
        assert result.storage["sessionStorage"] == {"sc_chat_effort": "low"}  # preferences survive
        assert result.requests == []

    def test_forget_key_clears_it(self, chat, tmp_path):
        result = run(
            chat,
            tmp_path,
            responses=[respond(DONE)],
            steps=[*conversation(Q1), click("sc-chat-forget"), ask(Q2), settle()],
        )
        assert len(result.requests) == 1
        assert result.final["ready"] is False
        assert "forgotten" in result.final["key_message"]

    @pytest.mark.parametrize("storage", ["throwing", "unavailable"])
    def test_restricted_storage_does_not_break_the_chat(self, chat, tmp_path, storage):
        result = run(
            chat,
            tmp_path,
            storage=storage,
            responses=[respond(DONE), respond(DONE)],
            steps=[
                open_chat(), save_key(), choose("sc-chat-model", "claude-sonnet-5"),
                ask(Q1), wait_idle(), ask(Q2), wait_idle(),
            ],
        )
        assert len(result.requests) == 2
        assert result.requests[0]["body"]["model"] == "claude-sonnet-5"
        assert result.errors() == []
        assert not _mentions_key(result)
        # The scenario really exercised a storage that fails: the page tried it.
        assert any(entry.get("failed") or entry.get("denied") for entry in result.storage_log)

    def test_a_stale_model_preference_falls_back_to_the_default(self, chat, tmp_path):
        result = run(
            chat,
            tmp_path,
            preload={"sessionStorage": {"sc_chat_model": "claude-sonnet-4-6"}},
            responses=[respond(DONE)],
            steps=conversation(Q1),
        )
        assert result.requests[0]["body"]["model"] == "claude-opus-5"

    def test_valid_preferences_are_still_remembered(self, chat, tmp_path):
        result = run(
            chat,
            tmp_path,
            preload={"sessionStorage": {"sc_chat_model": "claude-sonnet-5", "sc_chat_effort": "medium"}},
            responses=[respond(DONE)],
            steps=conversation(Q1),
        )
        body = result.requests[0]["body"]
        assert body["model"] == "claude-sonnet-5"
        assert body["output_config"] == {"effort": "medium"}
