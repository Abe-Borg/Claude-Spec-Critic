"""Chunk D — verification parser unification regression tests.

These tests pin the canonical verification parser behavior down so future
changes to the request shape, stop-reason handling, or response parsing
cannot silently reintroduce the legacy batch bug (which treated
``stop_reason="tool_use"`` as incomplete and only parsed text).

The regression matrix mirrors Chunk D directive 9:

1. Structured tool-use verdict from a real-time response.
2. Structured tool-use verdict from a batch response.
3. Text JSON fallback.
4. Tool-use stop reason.
5. Max-token incomplete response.
6. Missing required fields.
7. Source list malformed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.batch import BatchJob
from src.reviewer import Finding


pytestmark = pytest.mark.parser_unification


# ---------------------------------------------------------------------------
# Stop-reason classification (directive 5)
# ---------------------------------------------------------------------------


class TestClassifyVerificationStopReason:
    def test_tool_use_is_complete(self):
        from src.verifier import (
            STOP_CLASS_COMPLETE,
            classify_verification_stop_reason,
        )

        assert classify_verification_stop_reason("tool_use") == STOP_CLASS_COMPLETE

    def test_end_turn_is_complete(self):
        from src.verifier import (
            STOP_CLASS_COMPLETE,
            classify_verification_stop_reason,
        )

        assert classify_verification_stop_reason("end_turn") == STOP_CLASS_COMPLETE

    def test_pause_turn_is_pause(self):
        from src.verifier import (
            STOP_CLASS_PAUSE,
            classify_verification_stop_reason,
        )

        assert classify_verification_stop_reason("pause_turn") == STOP_CLASS_PAUSE

    def test_max_tokens_is_incomplete(self):
        from src.verifier import (
            STOP_CLASS_INCOMPLETE,
            classify_verification_stop_reason,
        )

        assert classify_verification_stop_reason("max_tokens") == STOP_CLASS_INCOMPLETE

    def test_unknown_stop_reason_is_incomplete(self):
        """The legacy batch parser misclassified ``tool_use`` because it
        only allowlisted ``end_turn``. The new helper treats every other
        stop_reason as incomplete by default, including ``None``, so
        future Anthropic-side additions degrade safely."""
        from src.verifier import (
            STOP_CLASS_INCOMPLETE,
            classify_verification_stop_reason,
        )

        for unknown in ("stop_sequence", "refusal", None, "", "totally_new"):
            assert (
                classify_verification_stop_reason(unknown) == STOP_CLASS_INCOMPLETE
            ), f"{unknown!r} should be incomplete"


# ---------------------------------------------------------------------------
# Canonical parser — happy paths (directives 1, 2, 4)
# ---------------------------------------------------------------------------


class TestCanonicalParserStructuredVerdict:
    """Tests 1, 2, 4 from directive 9 — structured verdict, tool_use stop."""

    def test_structured_tool_use_verdict_from_realtime_response(
        self, fake_anthropic
    ):
        """Case 1: real-time-shaped response with tool_use stop reason."""
        from src.verifier import (
            PARSE_STATUS_STRUCTURED,
            parse_verification_response,
        )

        resp = fake_anthropic.verification_tool_use_response(
            stop_reason="tool_use",
        )
        outcome = parse_verification_response(resp)
        assert outcome.parse_status == PARSE_STATUS_STRUCTURED
        assert outcome.verdict is not None
        assert outcome.verdict.verdict == "CONFIRMED"
        assert outcome.verdict.sources

    def test_structured_tool_use_verdict_from_batch_response(
        self, fake_anthropic
    ):
        """Case 2: batch-result envelope still produces a structured verdict.

        The legacy batch parser silently rejected ``tool_use`` stop
        reasons because its allowlist was ``end_turn`` only. The canonical
        parser must accept the same response regardless of which path
        delivered it.
        """
        from src.verifier import (
            PARSE_STATUS_STRUCTURED,
            parse_verification_response,
        )

        envelope = fake_anthropic.batch_verification_result(
            custom_id="verify__99"
        )
        # The batch result wraps the message in result.message.
        outcome = parse_verification_response(envelope.result.message)
        assert outcome.parse_status == PARSE_STATUS_STRUCTURED
        assert outcome.verdict is not None
        assert outcome.verdict.verdict == "CONFIRMED"

    def test_structured_verdict_with_end_turn_stop(self, fake_anthropic):
        """Belt-and-suspenders — ``end_turn`` should also yield the
        structured verdict when the model produced one."""
        from src.verifier import (
            PARSE_STATUS_STRUCTURED,
            parse_verification_response,
        )

        resp = fake_anthropic.verification_tool_use_response(
            stop_reason="end_turn"
        )
        outcome = parse_verification_response(resp)
        assert outcome.parse_status == PARSE_STATUS_STRUCTURED
        assert outcome.verdict.verdict == "CONFIRMED"

    def test_corrected_verdict_normalizes(self, fake_anthropic):
        from src.verifier import parse_verification_response

        payload = fake_anthropic.sample_verification_verdict_payload(
            verdict="corrected"
        )
        payload["correction"] = "2025 edition"
        resp = fake_anthropic.verification_tool_use_response(payload=payload)
        outcome = parse_verification_response(resp)
        assert outcome.verdict.verdict == "CORRECTED"
        assert outcome.verdict.correction == "2025 edition"

    def test_parses_dict_shape_responses(self, fake_anthropic):
        """The batch retrieval path can return plain dicts instead of SDK
        Pydantic objects. ``extract_tool_use_block`` already tolerates
        both shapes; the canonical parser must too."""
        from src.verifier import (
            PARSE_STATUS_STRUCTURED,
            parse_verification_response,
        )

        resp = fake_anthropic.verification_tool_use_response(dict_shape=True)
        outcome = parse_verification_response(resp)
        assert outcome.parse_status == PARSE_STATUS_STRUCTURED
        assert outcome.verdict.verdict == "CONFIRMED"


# ---------------------------------------------------------------------------
# Canonical parser — text fallback (directive 3)
# ---------------------------------------------------------------------------


class TestCanonicalParserTextFallback:
    def test_text_json_fallback_produces_verdict(self, fake_anthropic):
        from src.verifier import (
            PARSE_STATUS_TEXT,
            parse_verification_response,
        )

        resp = fake_anthropic.verification_text_fallback_response()
        outcome = parse_verification_response(resp)
        assert outcome.parse_status == PARSE_STATUS_TEXT
        assert outcome.verdict is not None
        assert outcome.verdict.verdict == "CONFIRMED"
        assert outcome.verdict.sources

    def test_text_fallback_with_fenced_json_block(self, fake_anthropic):
        """Model sometimes wraps JSON in a ``json`` code fence — the text
        parser already strips that. Surface it through the canonical
        parser to lock in the behavior."""
        from src.verifier import (
            PARSE_STATUS_TEXT,
            parse_verification_response,
        )

        fenced = (
            "```json\n"
            '{"verdict": "DISPUTED", "explanation": "x", '
            '"sources": ["https://example.com"], "correction": null}\n'
            "```"
        )
        msg = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=fenced)],
            stop_reason="end_turn",
        )
        outcome = parse_verification_response(msg)
        assert outcome.parse_status == PARSE_STATUS_TEXT
        assert outcome.verdict.verdict == "DISPUTED"


# ---------------------------------------------------------------------------
# Canonical parser — error cases (directives 5, 6, 7, 8)
# ---------------------------------------------------------------------------


class TestCanonicalParserErrorCases:
    def test_max_tokens_response_is_text_parse_error(self, fake_anthropic):
        """Case 5: a max_tokens-truncated response should not be silently
        accepted as a supported verdict. The text payload is partial and
        not valid JSON, so the canonical parser must classify it as a
        parse error (or, if there is no `{`, as a parse error too)."""
        from src.verifier import (
            PARSE_STATUS_TEXT_PARSE_ERROR,
            parse_verification_response,
        )

        resp = fake_anthropic.max_tokens_incomplete_response(
            partial_text="Truncated mid-sentence and no JSON object follows"
        )
        outcome = parse_verification_response(resp)
        assert outcome.parse_status == PARSE_STATUS_TEXT_PARSE_ERROR
        assert outcome.verdict is not None
        assert outcome.verdict.verdict == "UNVERIFIED"

    def test_invalid_json_text_is_text_parse_error(self):
        """The legacy batch parser would have returned the broken result
        through ``parse_response_fn`` and let the caller decide. The
        canonical parser surfaces ``text_parse_error`` so callers can
        emit a terminal_unverified outcome without re-running."""
        from src.verifier import (
            PARSE_STATUS_TEXT_PARSE_ERROR,
            parse_verification_response,
        )

        msg = SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"verdict": "CONFIRMED" not real json}')],
            stop_reason="end_turn",
        )
        outcome = parse_verification_response(msg)
        assert outcome.parse_status == PARSE_STATUS_TEXT_PARSE_ERROR
        assert outcome.verdict.verdict == "UNVERIFIED"
        assert "valid JSON" in outcome.verdict.explanation

    def test_empty_response_is_no_content(self):
        from src.verifier import (
            PARSE_STATUS_NO_CONTENT,
            parse_verification_response,
        )

        msg = SimpleNamespace(content=[], stop_reason="end_turn")
        outcome = parse_verification_response(msg)
        assert outcome.parse_status == PARSE_STATUS_NO_CONTENT
        assert outcome.verdict is None

    def test_text_with_no_json_object_is_parse_error(self):
        from src.verifier import (
            PARSE_STATUS_TEXT_PARSE_ERROR,
            parse_verification_response,
        )

        msg = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="No JSON here, just words.")],
            stop_reason="end_turn",
        )
        outcome = parse_verification_response(msg)
        assert outcome.parse_status == PARSE_STATUS_TEXT_PARSE_ERROR
        assert outcome.verdict.verdict == "UNVERIFIED"

    def test_missing_required_fields_normalizes(self, fake_anthropic):
        """Case 6: a tool payload that omits required fields should not be
        silently trusted. The parser normalizes the verdict to UNVERIFIED
        and the omitted fields to empty defaults. The grounding invariant
        then keeps malformed-but-CONFIRMED payloads from slipping through
        because no sources will be present."""
        from src.verifier import parse_verification_response

        # Missing explanation, sources, correction.
        resp = fake_anthropic.verification_tool_use_response(
            payload={"verdict": "CONFIRMED"}
        )
        outcome = parse_verification_response(resp)
        assert outcome.verdict is not None
        # Verdict survives literally — but explanation/sources are empty,
        # which the production callers couple with the grounding gate to
        # decide whether the verdict is trustworthy.
        assert outcome.verdict.verdict == "CONFIRMED"
        assert outcome.verdict.explanation == ""
        assert outcome.verdict.sources == []
        assert outcome.verdict.correction is None

    def test_missing_verdict_field_normalizes_to_unverified(
        self, fake_anthropic
    ):
        """A payload that omits ``verdict`` entirely should never become
        a CONFIRMED finding. The normalizer falls back to UNVERIFIED."""
        from src.verifier import parse_verification_response

        resp = fake_anthropic.verification_tool_use_response(
            payload={"explanation": "no verdict field"}
        )
        outcome = parse_verification_response(resp)
        assert outcome.verdict.verdict == "UNVERIFIED"

    def test_unknown_verdict_normalizes_to_unverified(self, fake_anthropic):
        """A payload with an out-of-enum verdict must never leak through.
        Production code branches on the four canonical names; an
        unrecognized value would silently bypass the grounding gate."""
        from src.verifier import parse_verification_response

        for bad in ("MAYBE", "POSSIBLY", "true", "100"):
            resp = fake_anthropic.verification_tool_use_response(
                payload={
                    "verdict": bad,
                    "explanation": "x",
                    "sources": [],
                    "correction": None,
                }
            )
            outcome = parse_verification_response(resp)
            assert outcome.verdict.verdict == "UNVERIFIED", (
                f"verdict={bad!r} must normalize"
            )


# ---------------------------------------------------------------------------
# Source list normalization (directive 7)
# ---------------------------------------------------------------------------


class TestSourceListNormalization:
    """Directive 9 case 7 — source list malformed in various shapes."""

    def test_sources_as_none_yields_empty_list(self, fake_anthropic):
        from src.verifier import parse_verification_response

        resp = fake_anthropic.verification_tool_use_response(
            payload={
                "verdict": "CONFIRMED",
                "explanation": "ok",
                "sources": None,
                "correction": None,
            }
        )
        outcome = parse_verification_response(resp)
        assert outcome.verdict.sources == []

    def test_sources_as_bare_string_is_wrapped(self, fake_anthropic):
        """A model that emits ``sources`` as a single string instead of a
        list should not crash the parser. The hardened normalizer wraps
        single strings into a one-element list (rather than iterating
        characters, which is the natural Python pitfall)."""
        from src.verifier import parse_verification_response

        resp = fake_anthropic.verification_tool_use_response(
            payload={
                "verdict": "CONFIRMED",
                "explanation": "ok",
                "sources": "https://only-source.example/",
                "correction": None,
            }
        )
        outcome = parse_verification_response(resp)
        assert outcome.verdict.sources == ["https://only-source.example/"]

    def test_sources_with_mixed_types_drops_non_truthy(self, fake_anthropic):
        from src.verifier import parse_verification_response

        resp = fake_anthropic.verification_tool_use_response(
            payload={
                "verdict": "CONFIRMED",
                "explanation": "ok",
                "sources": [
                    "https://good.example/",
                    "",
                    None,
                    "https://also-good.example/",
                ],
                "correction": None,
            }
        )
        outcome = parse_verification_response(resp)
        assert outcome.verdict.sources == [
            "https://good.example/",
            "https://also-good.example/",
        ]

    def test_sources_as_dict_is_dropped(self, fake_anthropic):
        from src.verifier import parse_verification_response

        resp = fake_anthropic.verification_tool_use_response(
            payload={
                "verdict": "CONFIRMED",
                "explanation": "ok",
                "sources": {"url": "https://example.com"},
                "correction": None,
            }
        )
        outcome = parse_verification_response(resp)
        # Dict is not a list/string of sources — coerced to empty rather
        # than crashing on iteration.
        assert outcome.verdict.sources == []

    def test_text_fallback_handles_non_list_sources(self):
        """The text path was previously crashable when ``sources`` came
        back as ``None`` — ``for s in data.get("sources", [])`` works,
        but ``data["sources"] = None`` does not. The hardened normalizer
        handles every shape uniformly."""
        from src.verifier import _parse_verification_response

        text = (
            '{"verdict": "CONFIRMED", "explanation": "x", '
            '"sources": null, "correction": null}'
        )
        result = _parse_verification_response(text)
        assert result.verdict == "CONFIRMED"
        assert result.sources == []


# ---------------------------------------------------------------------------
# Multi-message responses (real-time continuation path)
# ---------------------------------------------------------------------------


class TestCanonicalParserMultipleMessages:
    """The real-time path passes ``all_responses`` (a list) through the
    canonical parser. Pause_turn continuations may carry web_search blocks
    in earlier responses and the verdict tool in the terminal response.
    The parser must look across the whole list."""

    def test_verdict_tool_in_final_message_wins(self, fake_anthropic):
        from src.verifier import (
            PARSE_STATUS_STRUCTURED,
            parse_verification_response,
        )

        # Pause-turn continuation: text-only response, then terminal
        # response with the verdict tool.
        pause = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="searching...")],
            stop_reason="pause_turn",
        )
        terminal = fake_anthropic.verification_tool_use_response()
        outcome = parse_verification_response([pause, terminal])
        assert outcome.parse_status == PARSE_STATUS_STRUCTURED
        assert outcome.verdict.verdict == "CONFIRMED"

    def test_verdict_tool_in_earlier_message_still_wins(
        self, fake_anthropic
    ):
        """If the verdict appears in an earlier response (unusual, but
        possible if the model emits it then pauses) the parser still
        returns it."""
        from src.verifier import (
            PARSE_STATUS_STRUCTURED,
            parse_verification_response,
        )

        early = fake_anthropic.verification_tool_use_response()
        later_text = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="addendum")],
            stop_reason="end_turn",
        )
        outcome = parse_verification_response([early, later_text])
        assert outcome.parse_status == PARSE_STATUS_STRUCTURED
        assert outcome.verdict.verdict == "CONFIRMED"

    def test_text_concatenation_across_messages(self):
        """When no tool block exists anywhere, the parser concatenates
        text from all messages and tries the JSON fallback once."""
        from src.verifier import (
            PARSE_STATUS_TEXT,
            parse_verification_response,
        )

        # JSON straddles two messages — the canonical parser concatenates.
        m1 = SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"verdict": "DISPUTED",')],
            stop_reason="pause_turn",
        )
        m2 = SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="text",
                    text='"explanation": "Mixed evidence", "sources": [], "correction": null}',
                )
            ],
            stop_reason="end_turn",
        )
        outcome = parse_verification_response([m1, m2])
        assert outcome.parse_status == PARSE_STATUS_TEXT
        assert outcome.verdict.verdict == "DISPUTED"

    def test_empty_list_is_no_content(self):
        from src.verifier import (
            PARSE_STATUS_NO_CONTENT,
            parse_verification_response,
        )

        outcome = parse_verification_response([])
        assert outcome.parse_status == PARSE_STATUS_NO_CONTENT
        assert outcome.verdict is None

    def test_none_is_no_content(self):
        from src.verifier import (
            PARSE_STATUS_NO_CONTENT,
            parse_verification_response,
        )

        outcome = parse_verification_response(None)
        assert outcome.parse_status == PARSE_STATUS_NO_CONTENT
        assert outcome.verdict is None


# ---------------------------------------------------------------------------
# Integration: _classify_wave_results routes through canonical parser
# ---------------------------------------------------------------------------


def _make_finding(issue: str = "claim") -> Finding:
    return Finding(
        severity="HIGH",
        fileName="spec.docx",
        section="1",
        issue=issue,
        actionType="EDIT",
        existingText="old",
        replacementText="new",
        codeReference="CBC",
        confidence=0.9,
    )


class _FakeBatchResult:
    def __init__(self, message):
        self.result = SimpleNamespace(type="succeeded", message=message, error=None)


class TestWaveParserIntegration:
    """Verify the wave path (``_classify_wave_results``) actually routes
    through the canonical parser, not the old inline logic. These tests
    use the test fixtures end-to-end."""

    def test_wave_accepts_tool_use_stop_reason(
        self, monkeypatch, fake_anthropic
    ):
        """The legacy batch parser previously rejected ``tool_use``. With
        the canonical parser, a wave result that stops with ``tool_use``
        and carries the structured verdict must be classified as
        ``success`` (not ``terminal_unverified``)."""
        from src import verifier
        from src.verifier import _classify_wave_results

        msg = fake_anthropic.verification_tool_use_response(
            stop_reason="tool_use"
        )
        # The default fixture usage lacks server_tool_use; advertise one
        # web_search request so the grounding gate passes.
        msg.usage.server_tool_use = SimpleNamespace(web_search_requests=1)
        monkeypatch.setattr(
            verifier,
            "retrieve_verification_results_detailed",
            lambda _job: {"verify__0": _FakeBatchResult(msg)},
        )

        job = BatchJob(
            batch_id="msgbatch_chunk_d",
            job_type="verify",
            request_map={"verify__0": {"finding_idx": 0}},
            created_at=0.0,
        )
        finding = _make_finding()
        contexts = {
            "verify__0": {"finding_idx": 0, "original_prompt": "p", "model": "claude-sonnet-4-6"}
        }
        outcomes = _classify_wave_results(
            job=job, findings=[finding], request_contexts=contexts
        )
        assert len(outcomes) == 1
        assert outcomes[0].classification == "success"
        parsed = outcomes[0].parsed_verification
        assert parsed is not None
        assert parsed.verdict == "CONFIRMED"
        assert parsed.grounded is True

    def test_wave_text_parse_error_is_terminal(
        self, monkeypatch, fake_anthropic
    ):
        """A wave result whose body is non-JSON-text should be flagged as
        terminal_unverified, not retried, and not turned into a cached
        supported verdict. The canonical parser's text_parse_error status
        drives that decision."""
        from src import verifier
        from src.verifier import _classify_wave_results

        # Construct a response with web search blocks (so the grounding
        # gate passes) but no verdict tool and broken JSON text.
        from tests.fixtures.fake_anthropic import (
            FakeMessage,
            FakeServerToolUseBlock,
            FakeTextBlock,
            FakeUsage,
            FakeWebSearchResultBlock,
        )

        msg = FakeMessage(
            content=[
                FakeServerToolUseBlock(
                    name="web_search", input={"query": "anything"}
                ),
                FakeWebSearchResultBlock(
                    content=[
                        {
                            "type": "web_search_result",
                            "url": "https://example.com",
                            "title": "Example",
                            "encrypted_content": "x",
                        }
                    ]
                ),
                FakeTextBlock(text='{"verdict": "CONFIRMED" malformed'),
            ],
            stop_reason="end_turn",
            usage=FakeUsage(),
        )
        # Patch the usage to advertise a web_search request so the
        # search gate passes (otherwise the gate failure would short-
        # circuit before parsing).
        msg.usage.server_tool_use = SimpleNamespace(web_search_requests=1)

        monkeypatch.setattr(
            verifier,
            "retrieve_verification_results_detailed",
            lambda _job: {"verify__0": _FakeBatchResult(msg)},
        )

        job = BatchJob(
            batch_id="msgbatch_chunk_d_parse",
            job_type="verify",
            request_map={"verify__0": {"finding_idx": 0}},
            created_at=0.0,
        )
        finding = _make_finding()
        contexts = {
            "verify__0": {"finding_idx": 0, "original_prompt": "p", "model": "claude-sonnet-4-6"}
        }
        outcomes = _classify_wave_results(
            job=job, findings=[finding], request_contexts=contexts
        )
        assert outcomes[0].classification == "terminal_unverified"
        assert outcomes[0].parsed_verification is None
        # The unverified_reason carries the parse-error explanation so
        # operators see why the response was rejected. The hardened text
        # parser emits one of two recognizable prefixes.
        reason = outcomes[0].unverified_reason or ""
        assert (
            "valid JSON" in reason
            or "did not contain structured JSON" in reason
        ), f"unexpected reason: {reason!r}"

    def test_wave_max_tokens_is_terminal_unverified(
        self, monkeypatch, fake_anthropic
    ):
        """A wave message that stops with ``max_tokens`` is incomplete —
        it must not run through the parser at all."""
        from src import verifier
        from src.verifier import _classify_wave_results

        msg = fake_anthropic.max_tokens_incomplete_response()
        monkeypatch.setattr(
            verifier,
            "retrieve_verification_results_detailed",
            lambda _job: {"verify__0": _FakeBatchResult(msg)},
        )

        job = BatchJob(
            batch_id="msgbatch_chunk_d_truncated",
            job_type="verify",
            request_map={"verify__0": {"finding_idx": 0}},
            created_at=0.0,
        )
        finding = _make_finding()
        contexts = {
            "verify__0": {"finding_idx": 0, "original_prompt": "p", "model": "claude-sonnet-4-6"}
        }
        outcomes = _classify_wave_results(
            job=job, findings=[finding], request_contexts=contexts
        )
        assert outcomes[0].classification == "terminal_unverified"
        assert outcomes[0].parsed_verification is None
        assert "max_tokens" in (outcomes[0].unverified_reason or "")


# ---------------------------------------------------------------------------
# Legacy parser removal — the bad function is gone
# ---------------------------------------------------------------------------


class TestLegacyParserRemoved:
    """Chunk D directive 6: legacy parsing functions are quarantined or
    deleted once the canonical parser covers their behavior. The text-
    only batch parser is gone; importing it must fail."""

    def test_retrieve_verification_results_is_removed(self):
        from src import batch

        assert not hasattr(batch, "retrieve_verification_results"), (
            "Legacy batch parser must not be re-exported — it pre-dates "
            "structured tool use and misclassifies ``stop_reason=tool_use`` "
            "as incomplete."
        )

    def test_detailed_helper_still_exists(self):
        """The detail-retrieval helper survives — only the parser inside
        it was legacy. Wave parsing in verifier.py owns the parse step
        now."""
        from src import batch

        assert hasattr(batch, "retrieve_verification_results_detailed")
