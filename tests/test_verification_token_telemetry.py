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
* source inspection: the batch + real-time verification event payloads
  include the token keys.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.orchestration.diagnostics import DiagnosticsReport
from src.orchestration.resume_state import (
    deserialize_verification_result,
    serialize_verification_result,
)
from src.review.reviewer import Finding
from src.verification.verifier import (
    VerificationResult,
    _classify_wave_results,
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

    def test_missing_fields_default_to_zero(self):
        # A usage object that only reports one field still degrades safely.
        assert _token_usage(SimpleNamespace(usage=SimpleNamespace(input_tokens=10))) == (10, 0)


# ---------------------------------------------------------------------------
# 2. Batch wave parser stamps tokens onto the parsed result
# ---------------------------------------------------------------------------


def _grounded_message_with_tokens(input_tokens=120, output_tokens=60):
    msg = verification_tool_use_response(
        payload=sample_verification_verdict_payload(verdict="CONFIRMED")
    )
    # The wave parser's search gate needs a search count in usage; pair it
    # with the token counts the telemetry fix reads.
    msg.usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
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
                custom_id=cid, message=_grounded_message_with_tokens(120, 60)
            )},
        )
        outcomes = _classify_wave_results(job=job, findings=[f], request_contexts=ctx)
        assert len(outcomes) == 1
        parsed = outcomes[0].parsed_verification
        assert parsed is not None
        assert parsed.input_tokens == 120
        assert parsed.output_tokens == 60

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
# 3. Resume-state round-trip
# ---------------------------------------------------------------------------


class TestResumeRoundTrip:
    def test_tokens_round_trip(self):
        result = VerificationResult(verdict="CONFIRMED", grounded=True,
                                    sources=["https://x"], accepted_sources=["https://x"],
                                    input_tokens=222, output_tokens=33)
        restored = deserialize_verification_result(serialize_verification_result(result))
        assert restored is not None
        assert restored.input_tokens == 222
        assert restored.output_tokens == 33

    def test_legacy_payload_defaults_to_zero(self):
        # A state file written before the token fields existed loads as 0/0.
        payload = serialize_verification_result(
            VerificationResult(verdict="UNVERIFIED")
        )
        assert payload is not None
        payload.pop("input_tokens", None)
        payload.pop("output_tokens", None)
        restored = deserialize_verification_result(payload)
        assert restored is not None
        assert restored.input_tokens == 0
        assert restored.output_tokens == 0


# ---------------------------------------------------------------------------
# 4. Diagnostics aggregation picks up the tokens from a verification event
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 5. Source inspection — the event payloads carry the token keys
# ---------------------------------------------------------------------------


class TestEventPayloadCarriesTokens:
    def test_batch_controller_verification_event_includes_tokens(self):
        source = Path("src/gui/batch_controller.py").read_text(encoding="utf-8")
        assert '"input_tokens": f.verification.input_tokens' in source
        assert '"output_tokens": f.verification.output_tokens' in source
