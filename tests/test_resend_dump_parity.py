"""B-15: one Pydantic dump mode on the continuation resend path.

``verifier._content_block_to_plain`` (the wave parser's capture of a
``pause_turn`` assistant turn) and ``resend_sanitizer._to_plain_block`` (the
guard applied when that turn is re-sent) convert the *same* blocks. They must
dump identically — ``mode="json", exclude_none=True`` — because the
verifier's dicts go straight into batch request bodies: a non-JSON-native
value or an explicit ``null`` for an optional field is a submit-time
rejection.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel

import src.verification.verifier as V
from src.core import resend_sanitizer as RS


class _Kind(str, Enum):
    TEXT = "text"


class _Inner(BaseModel):
    url: str
    title: Optional[str] = None


class _Block(BaseModel):
    type: _Kind = _Kind.TEXT
    text: str
    citations: Optional[list] = None
    retrieved_at: datetime
    inner: _Inner


def _block() -> _Block:
    return _Block(
        text="hello",
        retrieved_at=datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc),
        inner=_Inner(url="https://codes.example.gov/x"),
    )


class TestOneDumpMode:
    def test_verifier_and_sanitizer_produce_identical_dicts(self) -> None:
        block = _block()
        assert V._content_block_to_plain(block) == RS._to_plain_block(block)

    def test_none_valued_optional_fields_are_dropped(self) -> None:
        plain = V._content_block_to_plain(_block())
        assert "citations" not in plain
        assert "title" not in plain["inner"]

    def test_non_json_native_fields_are_json_rendered(self) -> None:
        plain = V._content_block_to_plain(_block())
        assert plain["type"] == "text" and not isinstance(plain["type"], Enum)
        assert isinstance(plain["retrieved_at"], str)
        json.dumps(plain)  # must be directly serializable into a request body

    def test_real_sdk_block_parity(self) -> None:
        from anthropic.types import TextBlock

        block = TextBlock(type="text", text="hi", citations=None)
        verifier_plain = V._content_block_to_plain(block)
        assert verifier_plain == RS._to_plain_block(block)
        assert "citations" not in verifier_plain

    def test_dict_input_is_content_equal_on_both_paths(self) -> None:
        raw = {"type": "text", "text": "x", "nested": {"k": [1, 2]}}
        assert V._content_block_to_plain(raw) is raw  # verifier passes dicts through
        assert RS._to_plain_block(raw) == raw  # sanitizer deep-copies
        assert RS._to_plain_block(raw) is not raw

    def test_none_stays_none(self) -> None:
        assert V._content_block_to_plain(None) is None
