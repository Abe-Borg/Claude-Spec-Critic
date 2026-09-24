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

from src.orchestration.diagnostics import (
    DiagnosticsReport,
    record_verification_findings,
)
from src.review.reviewer import Finding
from src.verification.verifier import (
    VerificationResult,
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


def _verification(**overrides) -> VerificationResult:
    """A VerificationResult with only the fields these tests vary set."""
    defaults = dict(
        verdict="CONFIRMED",
        explanation="Checked against the published adoption table.",
        grounded=True,
        cache_status="miss",
        model_used="claude-sonnet-5",
        input_tokens=100,
        output_tokens=50,
    )
    defaults.update(overrides)
    return VerificationResult(**defaults)


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
        usage = _cache_token_usage(msg)
        assert usage["cache_creation_input_tokens"] == 4_000
        assert usage["cache_read_input_tokens"] == 12_000
        # A usage block reporting no per-TTL detail leaves the whole write
        # *unknown*, never a zero split — that is what keeps it priced at the
        # conservative 1-hour rate rather than silently free.
        assert usage["cache_creation_unknown_input_tokens"] == 4_000
        assert usage["cache_creation_breakdown_status"] == "absent"
        # A usage block without the cache keys (older fakes) reads as zeroes,
        # and the token helper keeps its two-tuple contract.
        for empty in (SimpleNamespace(usage=SimpleNamespace()), SimpleNamespace()):
            zeroed = _cache_token_usage(empty)
            assert zeroed["cache_creation_input_tokens"] == 0
            assert zeroed["cache_read_input_tokens"] == 0
            assert zeroed["cache_creation_breakdown_status"] == "none"
        assert _token_usage(msg) == (321, 99)

    def test_cache_counters_carry_the_provider_ttl_split(self):
        """When the provider breaks the write down, the split survives — it is
        the difference between pricing a token at 1.25x and at 2x."""
        msg = SimpleNamespace(usage=SimpleNamespace(
            input_tokens=1, output_tokens=1,
            cache_creation_input_tokens=1_000, cache_read_input_tokens=0,
            cache_creation=SimpleNamespace(
                ephemeral_5m_input_tokens=400, ephemeral_1h_input_tokens=600,
            ),
        ))
        usage = _cache_token_usage(msg)
        assert usage["cache_creation_5m_input_tokens"] == 400
        assert usage["cache_creation_1h_input_tokens"] == 600
        assert usage["cache_creation_unknown_input_tokens"] == 0
        assert usage["cache_creation_breakdown_status"] == "complete"

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
        # Plan WP-15: every result the verifier builds from a call carries its
        # attempt record — here the one batch conversation, identified by
        # the wave item that ended it — with the same usage as the flat
        # fields (which describe the kept call).
        assert len(parsed.call_usage) == 1
        attempt = parsed.call_usage[0]
        assert (attempt["operation"], attempt["role"], attempt["transport"]) == (
            "verification", "primary", "batch",
        )
        assert attempt["attempt_id"] == "batch:b:verify__0:primary"
        assert (attempt["input_tokens"], attempt["output_tokens"]) == (120, 60)
        assert attempt["cache_creation_input_tokens"] == 2_048
        assert attempt["cache_read_input_tokens"] == 8_192
        assert parsed.transport == "batch"

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


class TestVerificationEventShape:
    """The per-finding verification event, tested through the real recorder.

    This used to be a source pin against inline code in
    ``gui/batch_controller.py``. The block now lives in
    ``diagnostics.record_verification_findings`` — shared by the GUI and the
    headless driver — so the behavior is asserted directly and only the
    *delegation* is pinned by source (the GUI cannot be imported without
    tkinter).
    """

    def _finding_with(self, verification):
        return SimpleNamespace(
            fileName="21 10 00 - Water-Based.docx",
            severity="HIGH",
            confidence=0.8,
            verification=verification,
        )

    def test_event_carries_the_per_ttl_cache_split(self):
        """The counters ride through the shared reader, which is what keeps
        the per-TTL split from being dropped at this boundary."""
        report = DiagnosticsReport()
        verification = _verification(
            cache_creation_input_tokens=1_000,
            cache_read_input_tokens=2_000,
            cache_creation_5m_input_tokens=400,
            cache_creation_1h_input_tokens=600,
            cache_creation_unknown_input_tokens=0,
            cache_creation_breakdown_status="complete",
        )
        record_verification_findings(
            report,
            [self._finding_with(verification)],
            phase="verification",
            transport="batch",
        )
        event = next(e.data for e in report.events if (e.data or {}).get("api_call"))
        assert event["cache_creation_input_tokens"] == 1_000
        assert event["cache_read_input_tokens"] == 2_000
        assert event["cache_creation_5m_input_tokens"] == 400
        assert event["cache_creation_1h_input_tokens"] == 600
        assert event["cache_creation_breakdown_status"] == "complete"

    def test_escalated_result_carries_per_call_usage(self):
        """An escalated verification paid for TWO conversations on two
        models; the flat fields describe only the kept verdict's call, so the
        per-call list is what lets the cost summary price both."""
        report = DiagnosticsReport()
        verification = _verification()
        verification.escalation_attempted = True
        verification.call_usage = [
            {"model": "claude-sonnet-5", "escalated": False, "input_tokens": 10},
            {"model": "claude-opus-5", "escalated": True, "input_tokens": 20},
        ]
        record_verification_findings(
            report,
            [self._finding_with(verification)],
            phase="verification",
            transport="batch",
        )
        event = next(e.data for e in report.events if (e.data or {}).get("api_call"))
        # The per-call list is the event's billing input (plan WP-15), stored
        # as attempt records: each keeps its model and escalation role.
        assert [c["model"] for c in event["attempts"]] == [
            "claude-sonnet-5",
            "claude-opus-5",
        ]
        assert [c["role"] for c in event["attempts"]] == ["primary", "escalation"]

    def test_cache_hits_and_local_skips_are_not_counted_as_api_calls(self):
        """A replayed or locally-classified verdict ran no request, so it must
        contribute nothing to this run's call totals."""
        report = DiagnosticsReport()
        findings = [
            self._finding_with(_verification(cache_status="hit")),
            self._finding_with(_verification(cache_status="local_skip")),
            self._finding_with(_verification(cache_status="miss")),
        ]
        record_verification_findings(
            report, findings, phase="verification", transport="batch"
        )
        flags = [
            (e.data or {}).get("api_call")
            for e in report.events
            if (e.data or {}).get("verdict")
        ]
        assert flags == [False, False, True]

    def test_returns_the_verdict_tally(self):
        report = DiagnosticsReport()
        findings = [
            self._finding_with(_verification(verdict="CONFIRMED")),
            self._finding_with(_verification(verdict="CONFIRMED")),
            self._finding_with(_verification(verdict="UNVERIFIED")),
        ]
        tally = record_verification_findings(
            report, findings, phase="verification", transport="batch"
        )
        assert tally == {"CONFIRMED": 2, "UNVERIFIED": 1}

    def test_a_falsy_report_is_a_no_op(self):
        assert (
            record_verification_findings(
                None, [self._finding_with(_verification())],
                phase="verification", transport="batch",
            )
            == {}
        )

    def test_both_drivers_delegate_to_the_shared_recorder(self):
        """Source pin (the GUI cannot be imported without tkinter): neither
        driver may rebuild the event inline, or the two shapes drift and a
        phase priced on one goes unpriced on the other."""
        from pathlib import Path

        gui = Path("src/gui/batch_controller.py").read_text(encoding="utf-8")
        headless = Path("src/orchestration/pipeline.py").read_text(encoding="utf-8")
        for source in (gui, headless):
            assert "record_verification_findings(" in source
            assert "record_pass_api_call(" in source
            # The inline shape this replaced must not come back.
            assert "**cache_usage_from(f.verification)," not in source
        # Both verification rounds are recorded, on both drivers.
        for source in (gui, headless):
            assert 'phase="verification"' in source
            assert 'phase="cross_check_verification"' in source


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
