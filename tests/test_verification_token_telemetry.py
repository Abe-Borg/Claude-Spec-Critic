"""Verification token-usage telemetry.

Batch verification previously logged ``in=0/out=0`` in the per-phase
diagnostics because ``VerificationResult`` carried no token fields and the
batch parser never read ``message.usage``. These tests cover the fix:

* ``_token_usage`` reads input/output tokens defensively.
* ``_classify_wave_results`` stamps the tokens onto the parsed result.
* the fields round-trip through resume state.
* ``DiagnosticsReport.summary()`` aggregates the tokens into the
  verification phase when the event carries them (the shape the GUI
  controllers now emit).
"""
from __future__ import annotations

from types import SimpleNamespace

from src.orchestration.diagnostics import DiagnosticsReport
from src.review.reviewer import Finding
from src.verification.verifier import (
    _cache_token_usage,
    _classify_wave_results,
    _collect_conversation_evidence,
    _token_usage,
)
from tests.fixtures.fake_anthropic import (
    batch_verification_result,
    sample_verification_verdict_payload,
    verification_tool_use_response,
)


# ---------------------------------------------------------------------------
# 1. _token_usage helper
# ---------------------------------------------------------------------------


class TestTokenUsageHelper:
    def test_reads_input_and_output(self):
        msg = SimpleNamespace(usage=SimpleNamespace(input_tokens=321, output_tokens=99))
        assert _token_usage(msg) == (321, 99)

    def test_missing_usage_returns_zero(self):
        assert _token_usage(SimpleNamespace()) == (0, 0)
        assert _token_usage(SimpleNamespace(usage=None)) == (0, 0)

    def test_cache_counters_read_from_the_same_usage_block(self):
        msg = SimpleNamespace(usage=SimpleNamespace(
            input_tokens=321, output_tokens=99,
            cache_creation_input_tokens=4_000, cache_read_input_tokens=12_000,
        ))
        assert _cache_token_usage(msg) == (4_000, 12_000)
        # A usage block without the cache keys (older fakes) reads as 0/0,
        # and the token helper keeps its two-tuple contract.
        assert _cache_token_usage(SimpleNamespace(usage=SimpleNamespace())) == (0, 0)
        assert _cache_token_usage(SimpleNamespace()) == (0, 0)
        assert _token_usage(msg) == (321, 99)

    def test_conversation_evidence_sums_cache_counters_across_responses(self):
        def _resp(create, read):
            return SimpleNamespace(
                content=[],
                usage=SimpleNamespace(
                    input_tokens=10, output_tokens=5,
                    cache_creation_input_tokens=create, cache_read_input_tokens=read,
                ),
            )

        evidence = _collect_conversation_evidence([_resp(3_000, 0), _resp(0, 3_000)])
        assert (evidence.input_tokens, evidence.output_tokens) == (20, 10)
        assert evidence.cache_creation_input_tokens == 3_000
        assert evidence.cache_read_input_tokens == 3_000


# ---------------------------------------------------------------------------
# 2. Batch wave parser stamps tokens onto the parsed result
# ---------------------------------------------------------------------------


def _grounded_message_with_tokens(
    input_tokens=120, output_tokens=60, *, cache_create=0, cache_read=0
):
    msg = verification_tool_use_response(
        payload=sample_verification_verdict_payload(verdict="CONFIRMED")
    )
    # The wave parser's search gate needs a search count in usage; pair it
    # with the token counts the telemetry fix reads.
    msg.usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_create,
        cache_read_input_tokens=cache_read,
        server_tool_use=SimpleNamespace(web_search_requests=1, web_fetch_requests=0),
    )
    return msg


class TestWaveParserStampsTokens:
    def test_classify_wave_results_records_tokens(self, monkeypatch):
        import src.verification.verifier as V

        f = Finding(
            severity="HIGH", fileName="x.docx", section="2.1", issue="i",
            actionType="REPORT_ONLY", existingText=None, replacementText=None,
            confidence=0.5, codeReference="",
        )
        cid = "verify__0"
        ctx = {cid: {"finding_idx": 0, "model": "claude-sonnet-4-6", "escalated": False}}
        job = SimpleNamespace(
            batch_id="b",
            request_map={cid: {"finding_idx": 0, "model": "claude-sonnet-4-6"}},
            job_type="verify",
        )
        monkeypatch.setattr(
            V, "retrieve_verification_results_detailed",
            lambda _job: {cid: batch_verification_result(
                custom_id=cid,
                message=_grounded_message_with_tokens(
                    120, 60, cache_create=2_048, cache_read=8_192
                ),
            )},
        )
        outcomes = _classify_wave_results(job=job, findings=[f], request_contexts=ctx)
        assert len(outcomes) == 1
        parsed = outcomes[0].parsed_verification
        assert parsed is not None
        assert parsed.input_tokens == 120
        assert parsed.output_tokens == 60
        # The prompt-cache counters ride along so the cost summary can price
        # the cache write / read of the verification request.
        assert parsed.cache_creation_input_tokens == 2_048
        assert parsed.cache_read_input_tokens == 8_192
        # A single call carries no per-call list — the flat fields are it.
        assert parsed.call_usage == []

    def test_classify_wave_results_carries_raw_message(self, monkeypatch):
        """The success outcome retains the raw batch message (by identity, not
        a copy) so the deep-mode tracer can walk its thinking / tool blocks."""
        import src.verification.verifier as V

        f = Finding(
            severity="HIGH", fileName="x.docx", section="2.1", issue="i",
            actionType="REPORT_ONLY", existingText=None, replacementText=None,
            confidence=0.5, codeReference="",
        )
        cid = "verify__0"
        ctx = {cid: {"finding_idx": 0, "model": "claude-sonnet-4-6", "escalated": False}}
        job = SimpleNamespace(
            batch_id="b",
            request_map={cid: {"finding_idx": 0, "model": "claude-sonnet-4-6"}},
            job_type="verify",
        )
        msg = _grounded_message_with_tokens()
        monkeypatch.setattr(
            V, "retrieve_verification_results_detailed",
            lambda _job: {cid: batch_verification_result(custom_id=cid, message=msg)},
        )
        outcomes = _classify_wave_results(job=job, findings=[f], request_contexts=ctx)
        assert len(outcomes) == 1
        assert outcomes[0].classification == "success"
        assert outcomes[0].raw_message is msg


# ---------------------------------------------------------------------------
# 4. Diagnostics aggregation picks up the tokens from a verification event
# ---------------------------------------------------------------------------


class TestGuiEventShape:
    def test_batch_controller_emits_cache_counters_and_call_usage(self):
        """Source pin (no GUI import): the per-finding verification event
        the batch controller logs carries the cache counters and, for an
        escalated result, the per-call usage list the cost summary prices."""
        from pathlib import Path

        source = Path("src/gui/batch_controller.py").read_text(encoding="utf-8")
        assert '"cache_creation_input_tokens": getattr(' in source
        assert '"cache_read_input_tokens": getattr(' in source
        assert 'event_data["call_usage"] = [dict(c) for c in call_usage]' in source


class TestDiagnosticsAggregation:
    def test_verification_event_tokens_sum_into_phase(self):
        report = DiagnosticsReport()
        # Mirror the event shape the batch controller now emits per finding.
        report.log("verification", "info", "Verified: a.docx — CONFIRMED", {
            "verdict": "CONFIRMED",
            "api_call": True,
            "call_mode": "batch",
            "model": "claude-sonnet-4-6",
            "input_tokens": 100,
            "output_tokens": 40,
        })
        report.log("verification", "info", "Verified: b.docx — CORRECTED", {
            "verdict": "CORRECTED",
            "api_call": True,
            "call_mode": "batch",
            "model": "claude-sonnet-4-6",
            "input_tokens": 50,
            "output_tokens": 20,
        })
        s = report.summary()
        # Previously these were 0 because the keys were absent.
        assert s["total_input_tokens"] >= 150
        assert s["total_output_tokens"] >= 60
        ver = s["phase_telemetry"]["verification"]
        assert ver["input_tokens"] == 150
        assert ver["output_tokens"] == 60
