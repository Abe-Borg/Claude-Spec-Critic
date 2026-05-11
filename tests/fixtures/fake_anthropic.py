"""Fake Anthropic response builders for hermetic tests.

Chunk A baseline: the production parsers in ``src.reviewer``,
``src.batch``, and ``src.verifier`` consume objects that look like the
Anthropic SDK's Pydantic models — attribute access for ``content``,
``stop_reason``, ``usage``, ``content[i].type``, ``content[i].name``,
``content[i].input``, etc. The tagged-JSON / batch paths also accept plain
dicts. These builders return objects that satisfy both shapes so a single
fixture exercises both code paths.

Builders only emit data; they never hit the network. Pair them with a
``MagicMock``-style monkeypatch on ``messages.stream`` /
``messages.batches.results`` in tests that want to exercise the full
reviewer/verifier flow.

What's covered (the five cases called out in Chunk A directive 4):

1. ``review_tool_use_response`` — structured ``submit_review_findings`` tool call.
2. ``verification_tool_use_response`` — structured ``submit_verification_verdict`` tool call.
3. ``verification_tool_use_response`` — stop_reason ``tool_use`` (same call; see
   ``stop_reason`` kwarg) so callers can simulate the tool-use stop path.
4. ``verification_text_fallback_response`` — JSON-in-text fallback (no tool block).
5. ``max_tokens_incomplete_response`` — stop_reason ``max_tokens`` with partial text.

Each builder also supports a ``dict_shape`` flag so tests can exercise the
plain-dict code paths (batch retrieval can return either form).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Attribute-accessible stand-ins for SDK Pydantic models.
# ---------------------------------------------------------------------------


@dataclass
class FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeToolUseBlock:
    name: str
    input: dict[str, Any]
    id: str = "toolu_fake_1"
    type: str = "tool_use"


@dataclass
class FakeWebSearchResultBlock:
    """Mimic the ``web_search_tool_result`` block shape used by the SDK."""
    tool_use_id: str = "srvtoolu_fake_1"
    content: list[dict[str, Any]] = field(default_factory=list)
    type: str = "web_search_tool_result"


@dataclass
class FakeServerToolUseBlock:
    """A server-side tool invocation (e.g. ``web_search``)."""
    name: str
    input: dict[str, Any]
    id: str = "srvtoolu_fake_1"
    type: str = "server_tool_use"


@dataclass
class FakeMessage:
    """SDK-ish Message: attribute access on ``content``, ``stop_reason``, ``usage``."""
    content: list[Any]
    stop_reason: str = "end_turn"
    usage: FakeUsage = field(default_factory=FakeUsage)
    model: str = "claude-opus-4-7"
    id: str = "msg_fake_1"
    role: str = "assistant"
    type: str = "message"
    stop_sequence: str | None = None


# ---------------------------------------------------------------------------
# Canonical structured payloads (validate against schemas in tests).
# ---------------------------------------------------------------------------


def sample_review_findings_payload() -> dict[str, Any]:
    """Return a structured payload that matches ``REVIEW_FINDINGS_SCHEMA``.

    All schema-required keys are present so strict-mode validation passes.
    """
    return {
        "analysis_summary": "Reviewed Section 23 21 13. One stale code reference found.",
        "findings": [
            {
                "severity": "HIGH",
                "fileName": "23 21 13 - Hydronic.docx",
                "section": "2.1",
                "issue": "Cited California Plumbing Code edition is outdated for the 2025 cycle.",
                "actionType": "EDIT",
                "existingText": "per CPC 2022",
                "replacementText": "per CPC 2025",
                "codeReference": "CPC 2025",
                "confidence": 0.85,
                "anchorText": None,
                "insertPosition": None,
                # Chunk K3: the schema now requires evidenceElementId on
                # every finding (nullable). Fixture findings cite an id
                # so request-shape tests cover the populated path.
                "evidenceElementId": "p17",
            }
        ],
    }


def sample_verification_verdict_payload(
    *, verdict: str = "CONFIRMED", grounded_sources: list[str] | None = None
) -> dict[str, Any]:
    """Return a structured payload that matches ``VERIFICATION_VERDICT_SCHEMA``."""
    if grounded_sources is None:
        grounded_sources = ["https://www.dgs.ca.gov/DSA/"]
    return {
        "verdict": verdict,
        "explanation": "The 2025 California Plumbing Code is the current cycle per DSA.",
        "sources": grounded_sources,
        "correction": None,
    }


# ---------------------------------------------------------------------------
# Response builders (case 1 – case 5 from Chunk A directive 4).
# ---------------------------------------------------------------------------


def _maybe_dict(message: FakeMessage, *, dict_shape: bool) -> Any:
    if not dict_shape:
        return message
    return _to_dict(message)


def review_tool_use_response(
    *,
    payload: dict[str, Any] | None = None,
    stop_reason: str = "tool_use",
    dict_shape: bool = False,
    include_thinking_text: bool = False,
) -> Any:
    """Case 1: a successful structured ``submit_review_findings`` tool call.

    Mirrors the streaming + batch happy-path: a ``tool_use`` block whose
    ``input`` is the structured review payload, optionally preceded by a
    short text block (the model's pre-tool prose).
    """
    payload = payload if payload is not None else sample_review_findings_payload()
    content: list[Any] = []
    if include_thinking_text:
        content.append(FakeTextBlock(text="Reviewing the spec for code-cycle staleness..."))
    content.append(
        FakeToolUseBlock(name="submit_review_findings", input=dict(payload))
    )
    return _maybe_dict(
        FakeMessage(content=content, stop_reason=stop_reason), dict_shape=dict_shape
    )


def verification_tool_use_response(
    *,
    payload: dict[str, Any] | None = None,
    stop_reason: str = "tool_use",
    include_web_search_blocks: bool = True,
    dict_shape: bool = False,
) -> Any:
    """Cases 2 + 3: a structured ``submit_verification_verdict`` tool call.

    Defaults to ``stop_reason="tool_use"`` so this single builder also
    covers Chunk A directive 4 case 3 ("a verification response that stops
    with tool use"). Set ``stop_reason="end_turn"`` for the legacy path.

    When ``include_web_search_blocks=True`` (the default), the response
    also carries a ``server_tool_use`` block and a ``web_search_tool_result``
    block so grounding-detection helpers in ``verifier.py`` have something
    to match against.
    """
    payload = payload if payload is not None else sample_verification_verdict_payload()
    content: list[Any] = []
    if include_web_search_blocks:
        content.append(
            FakeServerToolUseBlock(
                name="web_search",
                input={"query": "California Plumbing Code 2025 effective date"},
            )
        )
        content.append(
            FakeWebSearchResultBlock(
                content=[
                    {
                        "type": "web_search_result",
                        "url": "https://www.dgs.ca.gov/DSA/",
                        "title": "DSA — California Code Adoptions",
                        "encrypted_content": "fake-encrypted-blob",
                    }
                ]
            )
        )
    content.append(
        FakeToolUseBlock(
            name="submit_verification_verdict", input=dict(payload)
        )
    )
    return _maybe_dict(
        FakeMessage(content=content, stop_reason=stop_reason), dict_shape=dict_shape
    )


def verification_text_fallback_response(
    *,
    payload: dict[str, Any] | None = None,
    stop_reason: str = "end_turn",
    dict_shape: bool = False,
) -> Any:
    """Case 4: a verification response that falls back to plain JSON text.

    No tool_use block — parsers must drop to ``_parse_verification_response``
    and pull the verdict out of the assistant text.
    """
    payload = payload if payload is not None else sample_verification_verdict_payload()
    body = json.dumps(payload)
    content: list[Any] = [FakeTextBlock(text=body)]
    return _maybe_dict(
        FakeMessage(content=content, stop_reason=stop_reason), dict_shape=dict_shape
    )


def max_tokens_incomplete_response(
    *,
    partial_text: str = "Reviewing… (output truncated mid-sentence",
    dict_shape: bool = False,
) -> Any:
    """Case 5: a response truncated by ``max_tokens``.

    The reviewer / batch retrieve paths treat any ``stop_reason`` other
    than ``end_turn`` or ``tool_use`` as incomplete; this fixture lets
    tests assert that the parse_status correctly degrades to ``incomplete``.
    """
    content: list[Any] = [FakeTextBlock(text=partial_text)]
    return _maybe_dict(
        FakeMessage(content=content, stop_reason="max_tokens"), dict_shape=dict_shape
    )


# ---------------------------------------------------------------------------
# Batch-result wrappers
# ---------------------------------------------------------------------------


@dataclass
class FakeBatchResultEnvelope:
    """Mimic the ``BatchResult.result`` inner type the SDK returns."""
    type: str = "succeeded"  # or "errored" / "expired" / "canceled"
    message: Any = None
    error: Any = None


@dataclass
class FakeBatchResult:
    """Mimic the outer batch result the SDK iterator yields."""
    custom_id: str
    result: FakeBatchResultEnvelope


def batch_review_result(
    custom_id: str = "review__SPEC__0",
    *,
    message: Any | None = None,
) -> FakeBatchResult:
    """Wrap a fake review response in a batch-result envelope."""
    if message is None:
        message = review_tool_use_response()
    return FakeBatchResult(
        custom_id=custom_id,
        result=FakeBatchResultEnvelope(type="succeeded", message=message),
    )


def batch_verification_result(
    custom_id: str = "verify__0",
    *,
    message: Any | None = None,
) -> FakeBatchResult:
    """Wrap a fake verification response in a batch-result envelope."""
    if message is None:
        message = verification_tool_use_response()
    return FakeBatchResult(
        custom_id=custom_id,
        result=FakeBatchResultEnvelope(type="succeeded", message=message),
    )


def batch_errored_result(
    custom_id: str = "review__SPEC__0",
    *,
    error_message: str = "fake error",
) -> FakeBatchResult:
    """Errored-request envelope, for failure-path tests."""
    error_obj = type("FakeError", (), {"message": error_message, "type": "api_error"})()
    return FakeBatchResult(
        custom_id=custom_id,
        result=FakeBatchResultEnvelope(type="errored", error=error_obj),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_dict(obj: Any) -> Any:
    """Recursively convert FakeMessage/etc into plain dicts (with no None keys for type)."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, list):
        return [_to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    if hasattr(obj, "__dataclass_fields__"):
        out: dict[str, Any] = {}
        for field_name in obj.__dataclass_fields__:
            out[field_name] = _to_dict(getattr(obj, field_name))
        return out
    return obj
