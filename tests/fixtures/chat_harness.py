"""Python side of the Ask AI chat harness (plan WP-12, chunk S04).

``chat_harness.js`` runs the HTML report's exact executable script under Node
and drives it like a reader. This module prepares its inputs and reads its
output:

* :func:`ship_chat` writes a real report with the production writer, extracts
  the executable script with the *same* regex the CSP tests use, checks that
  the extracted bytes are the ones the report's CSP hash covers, and describes
  the page (every element with an id, and the embedded data blocks) for the
  stand-in DOM.
* The SSE builders (:func:`sse`, :func:`reply` and the block helpers) produce
  the byte stream the Messages API sends, so tests control line endings,
  chunk boundaries, and multi-line ``data:`` frames exactly.
* :func:`run_chat` runs one scenario and returns a :class:`ChatRun`, failing
  the test loudly when the harness itself could not run it — a timeout, an
  unscripted request, or an exception the page let escape — so a broken
  probe never reads as a passing check.

Everything is hermetic: the fetch is scripted, no key is real, and every file
lives under the test's temporary directory.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

HARNESS_JS = Path(__file__).with_name("chat_harness.js")

#: A credential-shaped string that is obviously not a real key.
FAKE_KEY = "sk-ant-api03-FIXTURE-not-a-real-key-0123456789"

REQUIRE_TOOLS_ENV = "SPEC_CRITIC_REQUIRE_HTML_TEST_TOOLS"

_NODE_TIMEOUT_SECONDS = 60
_CSP_HASH_RE = re.compile(r"script-src '(sha256-[A-Za-z0-9+/=]+)'")
_ELEMENT_RE = re.compile(r'<([a-zA-Z][\w-]*)\b([^>]*?)\bid="([^"]+)"([^>]*)>')
_DATA_BLOCK_RE = re.compile(r'<script type="application/json" id="([^"]+)">(.*?)</script>', re.S)
_PLAINTEXT_RE = re.compile(r'<pre id="sc-plaintext" hidden>(.*?)</pre>', re.S)
_HIDDEN_ATTR_RE = re.compile(r"(?:^|\s)hidden(?=[\s>=/]|$)")
_TYPE_ATTR_RE = re.compile(r'\btype="([^"]*)"')


def node_or_skip() -> str:
    """Return the Node path, or skip / fail per the CI tooling policy."""
    node = shutil.which("node")
    if node:
        return node
    if os.environ.get(REQUIRE_TOOLS_ENV, "").strip().lower() not in ("", "0", "false", "no", "off"):
        pytest.fail(f"node is required when {REQUIRE_TOOLS_ENV} is set, but no `node` executable was found")
    pytest.skip(f"node not installed; set {REQUIRE_TOOLS_ENV}=1 to make this a failure")


@dataclass(frozen=True)
class ShippedChat:
    """A written report's executable script and page description, on disk."""

    script: str
    script_path: Path
    page_path: Path
    csp_hash: str


def ship_chat(directory: Path, builder=None) -> ShippedChat:
    """Write a real report and extract exactly what it ships."""
    from test_html_report_exporter import _EXEC_SCRIPT_RE, build_full_pipeline_result

    from src.output.html_report_exporter import write_html_report

    directory.mkdir(parents=True, exist_ok=True)
    report = directory / "report.html"
    write_html_report(
        (builder or build_full_pipeline_result)(),
        report,
        generated_at=datetime(2026, 1, 1, 12),
        include_chat=True,
    )
    text = report.read_bytes().decode("utf-8")
    scripts = _EXEC_SCRIPT_RE.findall(text)
    if len(scripts) != 1:
        pytest.fail(f"precondition: expected one executable script, found {len(scripts)}")
    script = scripts[0]
    declared = _CSP_HASH_RE.search(text)
    computed = "sha256-" + base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    if declared is None or declared.group(1) != computed:
        pytest.fail("precondition: the extracted script is not the one the report's CSP hash covers")

    elements = []
    for match in _ELEMENT_RE.finditer(text):
        tag, before, element_id, after = match.groups()
        attributes = f"{before} {after}"
        type_attr = _TYPE_ATTR_RE.search(attributes)
        elements.append(
            {
                "id": element_id,
                "tag": tag.lower(),
                "hidden": bool(_HIDDEN_ATTR_RE.search(attributes)),
                "type": type_attr.group(1) if type_attr else "",
            }
        )
    texts = {m.group(1): m.group(2) for m in _DATA_BLOCK_RE.finditer(text)}
    plaintext = _PLAINTEXT_RE.search(text)
    if plaintext is None or "sc-chat-config" not in texts or "sc-report-data" not in texts:
        pytest.fail("precondition: the report no longer carries the chat's data blocks")
    texts["sc-plaintext"] = html.unescape(plaintext.group(1))

    script_path = directory / "report-script.js"
    script_path.write_bytes(script.encode("utf-8"))
    page_path = directory / "page.json"
    page_path.write_text(json.dumps({"elements": elements, "text": texts}), encoding="utf-8")
    return ShippedChat(script=script, script_path=script_path, page_path=page_path, csp_hash=computed)


# ---------------------------------------------------------------------------
# The Messages API's stream, as bytes
# ---------------------------------------------------------------------------


def sse(events, *, newline: str = "\n", multiline: bool = False) -> bytes:
    """Encode events as SSE. ``multiline`` spreads each JSON payload over
    several ``data:`` lines (split between tokens, never inside a string)."""
    frames = []
    for event in events:
        payload = json.dumps(event, ensure_ascii=False, indent=1 if multiline else None)
        lines = [f"event: {event.get('type', 'message')}"] + [f"data: {line}" for line in payload.split("\n")]
        frames.append(newline.join(lines) + newline + newline)
    return "".join(frames).encode("utf-8")


def split_every(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)] or [b""]


def split_at(data: bytes, *offsets: int) -> list[bytes]:
    bounds = [0, *sorted(offsets), len(data)]
    return [data[a:b] for a, b in zip(bounds, bounds[1:])]


def _b64(chunk: bytes) -> str:
    return base64.b64encode(chunk).decode("ascii")


def message_start(model: str = "claude-opus-5", msg_id: str = "msg_test") -> dict:
    return {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 1},
        },
    }


def start(index: int, block: dict) -> dict:
    return {"type": "content_block_start", "index": index, "content_block": block}


def delta(index: int, body: dict) -> dict:
    return {"type": "content_block_delta", "index": index, "delta": body}


def stop(index: int) -> dict:
    return {"type": "content_block_stop", "index": index}


def text(*pieces: str, citations=(), citations_first: bool = True):
    """A text block streamed in ``pieces``, carrying ``citations`` deltas."""

    def events(index: int) -> list[dict]:
        cites = [delta(index, {"type": "citations_delta", "citation": c}) for c in citations]
        texts = [delta(index, {"type": "text_delta", "text": p}) for p in pieces]
        body = cites + texts if citations_first else texts + cites
        return [start(index, {"type": "text", "text": ""}), *body, stop(index)]

    return events


def thinking(summary: str, signature: str = "EqQBCgIYAhIMsig-fixture"):
    def events(index: int) -> list[dict]:
        return [
            start(index, {"type": "thinking", "thinking": ""}),
            delta(index, {"type": "thinking_delta", "thinking": summary}),
            delta(index, {"type": "signature_delta", "signature": signature}),
            stop(index),
        ]

    return events


def tool_call(tool_id: str, name: str, *json_pieces: str, kind: str = "tool_use"):
    """A tool_use (or server_tool_use) block whose input streams in pieces."""

    def events(index: int) -> list[dict]:
        pieces = [delta(index, {"type": "input_json_delta", "partial_json": p}) for p in json_pieces]
        return [start(index, {"type": kind, "id": tool_id, "name": name, "input": {}}), *pieces, stop(index)]

    return events


def whole(block: dict):
    """A block that arrives complete in its start event (a tool result)."""

    def events(index: int) -> list[dict]:
        return [start(index, block), stop(index)]

    return events


def ending(stop_reason: str | None = "end_turn", *, container=None, stop_details=None, message_stop: bool = True):
    body: dict = {"stop_reason": stop_reason, "stop_sequence": None}
    if container is not None:
        body["container"] = container
    if stop_details is not None:
        body["stop_details"] = stop_details
    events = [{"type": "message_delta", "delta": body, "usage": {"output_tokens": 7}}]
    if message_stop:
        events.append({"type": "message_stop"})
    return events


def reply(*blocks, stop_reason: str | None = "end_turn", model: str = "claude-opus-5", **ending_options) -> list[dict]:
    """A whole response: message_start, the blocks (indexed in order), the end."""
    events = [message_start(model)]
    for index, block in enumerate(blocks):
        events.extend(block(index))
    events.extend(ending(stop_reason, **ending_options))
    return events


# ---------------------------------------------------------------------------
# Scripted responses and steps
# ---------------------------------------------------------------------------


def respond(
    events=None,
    *,
    raw: bytes | None = None,
    newline: str = "\n",
    multiline: bool = False,
    chunk: int | None = None,
    cuts=None,
    **options,
) -> dict:
    """A 200 streamed response. ``chunk`` / ``cuts`` set the read boundaries;
    ``options`` pass through (``id``, ``manual``, ``hang``, ``read_error``,
    ``ignore_abort``)."""
    data = raw if raw is not None else sse(events or [], newline=newline, multiline=multiline)
    if chunk:
        chunks = split_every(data, chunk)
    elif cuts:
        chunks = split_at(data, *cuts)
    else:
        chunks = [data]
    return {"status": 200, "chunks_b64": [_b64(c) for c in chunks if c], **options}


def manual(response_id: str, *, ignore_abort: bool = False) -> dict:
    """A stream whose bytes arrive only through push / end / fail steps."""
    return {"status": 200, "id": response_id, "manual": True, "chunks_b64": [], "ignore_abort": ignore_abort}


def http_error(status: int, *, error_type: str = "invalid_request_error", message: str = "Bad request", body: str | None = None) -> dict:
    text_body = body if body is not None else json.dumps({"type": "error", "error": {"type": error_type, "message": message}})
    return {"status": status, "body_text": text_body}


def network_error(message: str = "Failed to fetch") -> dict:
    return {"network_error": message}


def open_chat() -> dict:
    return {"do": "open"}


def save_key(key: str = FAKE_KEY) -> dict:
    return {"do": "save_key", "key": key}


def ask(question: str, via: str = "click") -> dict:
    return {"do": "send", "text": question, "via": via}


def click(element_id: str) -> dict:
    return {"do": "click", "id": element_id}


def choose(element_id: str, value: str) -> dict:
    return {"do": "select", "id": element_id, "value": value}


def push(response_id: str, events=None, *, raw: bytes | None = None, chunk: int | None = None) -> dict:
    data = raw if raw is not None else sse(events or [])
    chunks = split_every(data, chunk) if chunk else [data]
    return {"do": "push", "response": response_id, "chunks_b64": [_b64(c) for c in chunks if c]}


def end_stream(response_id: str) -> dict:
    return {"do": "end", "response": response_id}


def fail_stream(response_id: str, message: str = "network error") -> dict:
    return {"do": "fail", "response": response_id, "message": message}


def wait_idle() -> dict:
    return {"do": "wait_idle"}


def wait_requests(count: int) -> dict:
    return {"do": "wait_requests", "count": count}


def wait_text(fragment: str) -> dict:
    return {"do": "wait_text", "text": fragment}


def settle(ticks: int = 20) -> dict:
    return {"do": "settle", "ticks": ticks}


def snapshot(label: str) -> dict:
    return {"do": "snapshot", "label": label}


def pagehide() -> dict:
    return {"do": "pagehide"}


def conversation(*questions: str) -> list[dict]:
    """Open the chat, save the fake key, and ask each question in turn."""
    steps = [open_chat(), save_key()]
    for question in questions:
        steps += [ask(question), wait_idle()]
    return steps


# ---------------------------------------------------------------------------
# Running a scenario
# ---------------------------------------------------------------------------


@dataclass
class ChatRun:
    raw: dict

    @property
    def requests(self) -> list[dict]:
        return self.raw["requests"]

    def sent(self, n: int) -> list[dict]:
        """The ``messages`` array of request ``n`` (0-based)."""
        return self.requests[n]["body"]["messages"]

    @property
    def final(self) -> dict:
        return self.raw["final"]

    def snapshot(self, label: str) -> dict:
        found = [s for s in self.raw["snapshots"] if s["label"] == label]
        assert len(found) == 1, f"expected one snapshot labelled {label!r}"
        return found[0]

    def messages(self, state: dict | None = None) -> list[dict]:
        return (state or self.final)["messages"]

    def of_kind(self, kind: str, state: dict | None = None) -> list[dict]:
        return [m for m in self.messages(state) if f"sc-msg-{kind}" in m["classes"]]

    def notices(self, state: dict | None = None) -> list[str]:
        return [m["text"] for m in self.of_kind("notice", state)]

    def errors(self, state: dict | None = None) -> list[str]:
        return [m["text"] for m in self.of_kind("error", state)]

    def answers(self, state: dict | None = None) -> list[dict]:
        return self.of_kind("assistant", state)

    def assert_idle(self, state: dict | None = None) -> None:
        state = state or self.final
        assert state["send_disabled"] is False, "the Send button was left disabled"
        assert state["stop_hidden"] is True, "the Stop button was left visible"
        assert state["input_disabled"] is False, "the message box was left disabled"

    @property
    def storage_log(self) -> list[dict]:
        return self.raw["storage_log"]

    @property
    def storage(self) -> dict:
        return self.raw["storage"]

    @property
    def effects(self) -> list[dict]:
        return self.raw["effects"]

    @property
    def streams(self) -> list[dict]:
        return self.raw["streams"]


def run_chat(
    chat: ShippedChat,
    directory: Path,
    *,
    responses=(),
    steps=(),
    storage: str = "normal",
    preload: dict | None = None,
    allow_unused_responses: bool = False,
) -> ChatRun:
    node = node_or_skip()
    directory.mkdir(parents=True, exist_ok=True)
    scenario_path = directory / "scenario.json"
    scenario_path.write_text(
        json.dumps({"storage": storage, "preload": preload or {}, "responses": list(responses), "steps": list(steps)}),
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            [node, str(HARNESS_JS), str(chat.script_path), str(chat.page_path), str(scenario_path)],
            capture_output=True,
            encoding="utf-8",
            timeout=_NODE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:  # pragma: no cover - environment failure
        pytest.fail(f"the chat harness timed out after {_NODE_TIMEOUT_SECONDS}s")
    if proc.returncode != 0 or not proc.stdout.strip():
        pytest.fail(f"the chat harness did not run (exit {proc.returncode}): {proc.stderr[-3000:]}")
    result = json.loads(proc.stdout)
    if not result.get("ok"):
        pytest.fail(f"the shipped script did not load under the harness: {result}")
    if result["problems"]:
        pytest.fail("the harness could not complete the scenario: " + "; ".join(result["problems"]))
    if result["page_errors"]:
        pytest.fail("the page let an exception escape: " + "\n".join(result["page_errors"]))
    if result["unused_responses"] and not allow_unused_responses:
        pytest.fail(f"{result['unused_responses']} scripted response(s) were never requested")
    return ChatRun(result)
