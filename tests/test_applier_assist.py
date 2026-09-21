"""The assist tier: what it may decide, and everything it may not.

Hermetic — the client is injected and scripted, so these run with no key and
no network. What they pin is the blast radius of a wrong answer: assist can
put a correct edit in the wrong *place*, and that is all. It cannot author
text, cannot reach an element it was not shown, and cannot rescue a target
whose text is gone.
"""
from __future__ import annotations

import pytest

from applier.assist import AssistConfig, AssistUnavailable, assist_location, build_client
from applier.locator import classify_element_id
from applier.models import Candidate, EditEntry, Location, LocationStatus


class Block:
    def __init__(self, name, payload, block_id="tu_1"):
        self.type = "tool_use"
        self.name = name
        self.input = payload
        self.id = block_id


class TextBlock:
    def __init__(self, text="thinking out loud"):
        self.type = "text"
        self.text = text


class Message:
    def __init__(self, content):
        self.content = content


class ScriptedClient:
    """Returns the next scripted message per call and records the requests."""

    def __init__(self, script):
        self._script = list(script)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self._script:
            raise AssertionError("the assist loop made more calls than scripted")
        nxt = self._script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def candidate(element_id, text, section_id=""):
    return Candidate(
        element_id=element_id,
        text=text,
        section_id=section_id,
        element_type="paragraph",
        kind=classify_element_id(element_id),
    )


def entry(**overrides):
    base = dict(
        finding_id="rf-1",
        file_name="215000.docx",
        action_type="EDIT",
        existing_text="NFPA 13, 2019 edition",
        replacement_text="NFPA 13, 2025 edition",
        anchor_text=None,
        insert_position=None,
        target_element_id=None,
        evidence_element_id=None,
        edit_confidence=0.9,
        report_status="VERIFIED_SUPPORTED",
        verification_verdict="CONFIRMED",
        severity="HIGH",
        section="21 13 13",
        issue="…",
        code_reference=None,
        has_per_file_original=True,
    )
    base.update(overrides)
    return EditEntry(**base)


CANDIDATES = [
    candidate("p1", "Sprinklers shall comply with NFPA 13, 2019 edition.", "PART 1"),
    candidate("p9", "Hangers shall comply with NFPA 13, 2019 edition.", "PART 2"),
    candidate("p12", "Unrelated clause."),
]

AMBIGUOUS = Location(
    status=LocationStatus.AMBIGUOUS,
    detail="2 elements contain the target text",
    candidates=(CANDIDATES[0], CANDIDATES[1]),
)

CONFIG = AssistConfig(enabled=True, model="test-model")


def assist(location, script, candidates=CANDIDATES, the_entry=None):
    client = ScriptedClient(script)
    result = assist_location(
        the_entry or entry(),
        candidates,
        location,
        client=client,
        config=CONFIG,
    )
    return result, client


class TestDisambiguation:
    def test_a_valid_choice_resolves_the_refusal(self):
        location, _ = assist(
            AMBIGUOUS,
            [Message([Block("choose_element", {"element_id": "p9", "reasoning": "hangers"})])],
        )
        assert location.status is LocationStatus.RESOLVED_BY_ASSIST
        assert location.element_id == "p9"
        assert location.is_applicable
        assert "hangers" in location.detail

    def test_the_tools_are_usable_before_choosing(self):
        location, client = assist(
            AMBIGUOUS,
            [
                Message([Block("search_document", {"query": "hangers"})]),
                Message([Block("read_element", {"element_id": "p9"})]),
                Message([Block("choose_element", {"element_id": "p9", "reasoning": "ok"})]),
            ],
        )
        assert location.element_id == "p9"
        assert len(client.requests) == 3
        # The conversation grows: tool results are fed back as real turns.
        assert len(client.requests[-1]["messages"]) == 5

    def test_declining_leaves_the_original_refusal_intact(self):
        location, _ = assist(
            AMBIGUOUS,
            [Message([Block("decline", {"reason": "both clauses are plausible"})])],
        )
        assert location.status is LocationStatus.AMBIGUOUS
        assert "both clauses are plausible" in location.detail
        assert not location.is_applicable


class TestAHallucinatedChoiceIsDropped:
    def test_an_element_id_that_does_not_exist(self):
        location, _ = assist(
            AMBIGUOUS,
            [Message([Block("choose_element", {"element_id": "p404", "reasoning": "x"})])],
        )
        assert location.status is LocationStatus.AMBIGUOUS
        assert "not among the elements it was shown" in location.detail

    def test_an_element_that_exists_but_was_not_offered(self):
        """p12 is in the document but was not one of the ambiguous candidates,
        so choosing it means the model ignored the question it was asked."""
        location, _ = assist(
            AMBIGUOUS,
            [Message([Block("choose_element", {"element_id": "p12", "reasoning": "x"})])],
        )
        assert location.status is LocationStatus.AMBIGUOUS
        assert "not among the elements it was shown" in location.detail

    def test_a_choice_whose_text_no_longer_confirms_the_target(self):
        stale = Location(
            status=LocationStatus.AMBIGUOUS,
            detail="ambiguous",
            candidates=(CANDIDATES[0], candidate("p20", "No matching text here.")),
        )
        location, _ = assist(
            stale,
            [Message([Block("choose_element", {"element_id": "p20", "reasoning": "x"})])],
            candidates=CANDIDATES + [candidate("p20", "No matching text here.")],
        )
        assert location.status is LocationStatus.AMBIGUOUS
        assert "no longer confirms the target text" in location.detail

    def test_an_unsupported_container_is_never_chosen(self):
        note = candidate("fn1p0", "Sprinklers shall comply with NFPA 13, 2019 edition.")
        stale = Location(
            status=LocationStatus.AMBIGUOUS,
            detail="ambiguous",
            candidates=(CANDIDATES[0], note),
        )
        location, _ = assist(
            stale,
            [Message([Block("choose_element", {"element_id": "fn1p0", "reasoning": "x"})])],
            candidates=CANDIDATES + [note],
        )
        assert location.status is LocationStatus.AMBIGUOUS


class TestAssistNeverRescuesDrift:
    @pytest.mark.parametrize(
        "status", [LocationStatus.DRIFTED, LocationStatus.NOT_FOUND]
    )
    def test_a_suggestion_is_reported_but_never_applied(self, status):
        original = Location(status=status, detail="the text is gone", element_id="p4")
        location, _ = assist(
            original,
            [Message([Block("choose_element", {"element_id": "p9", "reasoning": "moved"})])],
        )
        assert location.status is status
        assert not location.is_applicable
        assert "assist suggests the clause moved to p9" in location.detail
        assert "not applied" in location.detail


class TestBounds:
    def test_the_tool_round_cap_terminates_the_loop(self):
        config = AssistConfig(enabled=True, model="test-model", max_tool_rounds=2)
        client = ScriptedClient(
            [
                Message([Block("search_document", {"query": "a"})]),
                Message([Block("search_document", {"query": "b"})]),
            ]
        )
        location = assist_location(
            entry(), CANDIDATES, AMBIGUOUS, client=client, config=config
        )
        assert location.status is LocationStatus.AMBIGUOUS
        assert "exceeded 2 tool rounds" in location.detail
        assert len(client.requests) == 2

    def test_an_api_failure_is_absorbed_not_raised(self):
        location, _ = assist(AMBIGUOUS, [RuntimeError("503 overloaded")])
        assert location.status is LocationStatus.AMBIGUOUS
        assert "assist call failed" in location.detail

    def test_a_reply_with_no_tool_call_ends_the_loop(self):
        location, _ = assist(AMBIGUOUS, [Message([TextBlock()])])
        assert location.status is LocationStatus.AMBIGUOUS
        assert "without calling a tool" in location.detail

    def test_an_unknown_tool_name_does_not_derail_the_loop(self):
        location, _ = assist(
            AMBIGUOUS,
            [
                Message([Block("rewrite_specification", {"text": "hostile"})]),
                Message([Block("choose_element", {"element_id": "p1", "reasoning": "ok"})]),
            ],
        )
        assert location.element_id == "p1"


class TestScope:
    @pytest.mark.parametrize(
        "status",
        [
            LocationStatus.RESOLVED_BY_ID,
            LocationStatus.RESOLVED_BY_UNIQUE_TEXT,
            LocationStatus.RESOLVED_BY_SECTION,
            LocationStatus.UNSUPPORTED_ELEMENT,
        ],
    )
    def test_assist_is_not_consulted_for_a_settled_or_unwritable_location(self, status):
        original = Location(status=status, element_id="p1")
        client = ScriptedClient([])  # any call would raise
        assert (
            assist_location(
                entry(), CANDIDATES, original, client=client, config=CONFIG
            )
            is original
        )
        assert client.requests == []


class TestThePromptConstrainsTheModel:
    def test_the_only_terminal_tools_are_choose_and_decline(self):
        client = ScriptedClient(
            [Message([Block("choose_element", {"element_id": "p1", "reasoning": "x"})])]
        )
        assist_location(entry(), CANDIDATES, AMBIGUOUS, client=client, config=CONFIG)
        tools = {tool["name"] for tool in client.requests[0]["tools"]}
        assert tools == {
            "search_document",
            "read_element",
            "choose_element",
            "decline",
        }

    def test_no_tool_accepts_specification_text(self):
        """The model has no channel through which a word it wrote can land in
        the document — the replacement always comes from the sidecar."""
        client = ScriptedClient(
            [Message([Block("choose_element", {"element_id": "p1", "reasoning": "x"})])]
        )
        assist_location(entry(), CANDIDATES, AMBIGUOUS, client=client, config=CONFIG)
        for tool in client.requests[0]["tools"]:
            properties = set(tool["input_schema"]["properties"])
            assert not properties & {
                "replacement_text",
                "text",
                "new_text",
                "content",
            }

    def test_the_system_prompt_states_the_location_only_rule(self):
        client = ScriptedClient(
            [Message([Block("choose_element", {"element_id": "p1", "reasoning": "x"})])]
        )
        assist_location(entry(), CANDIDATES, AMBIGUOUS, client=client, config=CONFIG)
        system = client.requests[0]["system"]
        assert "LOCATION only" in system
        assert "untrusted" in system.lower()

    def test_document_text_reaches_the_model_only_through_tool_results(self):
        client = ScriptedClient(
            [Message([Block("choose_element", {"element_id": "p1", "reasoning": "x"})])]
        )
        assist_location(entry(), CANDIDATES, AMBIGUOUS, client=client, config=CONFIG)
        first_user = client.requests[0]["messages"][0]["content"]
        # Only the ambiguous candidates are volunteered, not the whole spec.
        assert "Unrelated clause." not in first_user


class TestClientConstruction:
    def test_a_missing_key_is_an_actionable_refusal(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        monkeypatch.setattr(
            "src.core.api_key_store.load_api_key_from_file", lambda: ""
        )
        with pytest.raises(AssistUnavailable) as excinfo:
            build_client()
        assert "ANTHROPIC_API_KEY" in str(excinfo.value)

    def test_an_explicit_key_is_used(self):
        assert build_client("sk-ant-test-key") is not None

    def test_assist_is_off_by_default(self):
        assert AssistConfig().enabled is False
