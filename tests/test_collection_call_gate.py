"""The collection-call permit is held per API call, not per pass (B-12).

A routed program shares one ``SPEC_CRITIC_REALTIME_COLLECTION_CALLS`` pool
across its module collectors. Realtime verification already took a permit per
call, but the headless driver wrapped the *whole* cross-check, compliance, and
drawing-impact passes in one ``with`` — so a chunked cross-check of five-plus
calls (with backoff sleeps between them) held a single permit throughout.
The pool is now threaded down as a ``call_gate`` that each runner acquires
around exactly one streaming call and releases before parsing and before any
backoff sleep. A single-module run passes no gate and behaves as before.
"""
from __future__ import annotations

import pytest

import src.cross_check.cross_checker as cc
import src.drawing_impact as di_pkg
import src.drawing_impact.impact_synthesizer as impact
from src.batch.batch import BatchJob
from src.core.code_cycles import DEFAULT_CYCLE
from src.cross_check.cross_checker import run_chunked_cross_check, run_cross_check
from src.drawing_impact import DrawingImpactResult, run_drawing_impact
from src.gui.context_attachment import wrap_attachment
from src.input.drawing_digest import DIGEST_ATTACHMENT_LABEL
from src.input.extractor import ExtractedSpec
from src.orchestration import pipeline as pl
from src.orchestration.pipeline import (
    BatchSubmission,
    CollectedBatchState,
    run_cross_check_for_batch,
    run_drawing_impact_for_batch,
)
from src.review.reviewer import ReviewResult
from src.review.structured_schemas import CROSS_CHECK_TOOL_NAME, DRAWING_IMPACT_TOOL_NAME
from src.verification.verification_cache import VerificationCache
from tests.fixtures.fake_anthropic import FakeMessage, FakeToolUseBlock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class CountingGate:
    """A non-reentrant context manager that records how it was used."""

    def __init__(self) -> None:
        self.acquisitions = 0
        self.held = False
        self.reentered = False

    def __enter__(self):
        if self.held:
            self.reentered = True
        self.held = True
        self.acquisitions += 1
        return self

    def __exit__(self, *_args):
        self.held = False
        return False


class _FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @property
    def text_stream(self):
        return iter(())

    def get_final_message(self):
        return self._message


class _FakeMessages:
    """Scripted ``messages.stream``; records the gate state at each call."""

    def __init__(self, script: list, gate: CountingGate):
        self._script = list(script)
        self._gate = gate
        self.calls: list[dict] = []
        self.held_at_call: list[bool] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        self.held_at_call.append(self._gate.held)
        assert self._script, "script exhausted: more calls than planned"
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeStream(item)


class _FakeClient:
    def __init__(self, script: list, gate: CountingGate):
        self.messages = _FakeMessages(script, gate)

    @property
    def calls(self):
        return self.messages.calls

    @property
    def held_at_call(self):
        return self.messages.held_at_call


def _cross_check_ok() -> FakeMessage:
    return FakeMessage(
        content=[
            FakeToolUseBlock(
                name=CROSS_CHECK_TOOL_NAME,
                input={"findings": [], "coordination_summary": "Coordination appears adequate."},
            )
        ],
        stop_reason="tool_use",
    )


def _impact_ok() -> FakeMessage:
    return FakeMessage(
        content=[
            FakeToolUseBlock(
                name=DRAWING_IMPACT_TOOL_NAME,
                input={
                    "impact_level": "minimal",
                    "narrative": "The drawings added little.",
                    "finding_links": [],
                },
            )
        ],
        stop_reason="tool_use",
    )


# Three CSI divisions with two specs each → three chunks, no singleton pooling.
_CHUNKED_FILENAMES = [
    "21 13 13 Wet-Pipe.docx",
    "21 13 16 Dry-Pipe.docx",
    "22 11 13 Domestic Water.docx",
    "22 11 16 Piping.docx",
    "23 05 00 HVAC Common.docx",
    "23 21 13 Hydronic.docx",
]


def _chunked_specs() -> list[ExtractedSpec]:
    return [
        ExtractedSpec(
            filename=name,
            content=f"SPECTOKEN SPECTOKEN Provide equipment for {name} per code.",
            word_count=8,
        )
        for name in _CHUNKED_FILENAMES
    ]


@pytest.fixture
def force_three_chunks(monkeypatch):
    """Token stub that counts spec markers only: the full corpus (12) exceeds
    the patched limit (5) so the pass chunks, while each two-spec chunk (4)
    fits and runs."""
    monkeypatch.setattr(cc, "count_tokens", lambda text: text.count("SPECTOKEN"))
    monkeypatch.setattr(cc, "CROSS_CHECK_RECOMMENDED_MAX", 5)


# ---------------------------------------------------------------------------
# Cross-check: one permit per API call, released across backoff
# ---------------------------------------------------------------------------


class TestCrossCheckGate:
    def test_three_chunk_pass_takes_one_permit_per_call(self, monkeypatch, force_three_chunks):
        gate = CountingGate()
        client = _FakeClient([_cross_check_ok()] * 3, gate)
        monkeypatch.setattr(cc, "_get_client", lambda *_a, **_k: client)

        result = run_chunked_cross_check(
            _chunked_specs(), [], cycle=DEFAULT_CYCLE, call_gate=gate
        )

        assert result.cross_check_status == "completed"
        assert len(client.calls) == 3
        assert gate.acquisitions == len(client.calls)
        assert client.held_at_call == [True, True, True]
        assert gate.held is False
        assert gate.reentered is False

    def test_gate_is_never_held_across_a_backoff_sleep(self, monkeypatch):
        monkeypatch.setattr(cc, "count_tokens", lambda text: len(text.split()))
        gate = CountingGate()
        # Transient (retryable) failure first, then a clean payload.
        client = _FakeClient(
            [RuntimeError("connection reset by peer"), _cross_check_ok()], gate
        )
        monkeypatch.setattr(cc, "_get_client", lambda *_a, **_k: client)
        held_during_sleep: list[bool] = []
        monkeypatch.setattr(cc.time, "sleep", lambda _s: held_during_sleep.append(gate.held))

        result = run_cross_check(_chunked_specs()[:2], [], cycle=DEFAULT_CYCLE, call_gate=gate)

        assert result.cross_check_status == "completed"
        assert held_during_sleep == [False]
        assert gate.acquisitions == 2 == len(client.calls)
        assert client.held_at_call == [True, True]
        assert gate.held is False

    def test_no_gate_is_the_ungated_path(self, monkeypatch):
        monkeypatch.setattr(cc, "count_tokens", lambda text: len(text.split()))
        gate = CountingGate()  # only observes; never passed in
        client = _FakeClient([_cross_check_ok()], gate)
        monkeypatch.setattr(cc, "_get_client", lambda *_a, **_k: client)

        result = run_cross_check(_chunked_specs()[:2], [], cycle=DEFAULT_CYCLE)

        assert result.cross_check_status == "completed"
        assert gate.acquisitions == 0


# ---------------------------------------------------------------------------
# Drawing impact: the synthesizer honors the same contract
# ---------------------------------------------------------------------------


class TestDrawingImpactGate:
    def test_synthesis_call_takes_one_permit(self):
        gate = CountingGate()
        client = _FakeClient([_impact_ok()], gate)

        result = run_drawing_impact(
            digest_text="DIGEST [plans.pdf p.1]", findings=[], client=client, call_gate=gate
        )

        assert result.status == "completed"
        assert gate.acquisitions == 1 == len(client.calls)
        assert client.held_at_call == [True]
        assert gate.held is False

    def test_gate_released_across_backoff(self, monkeypatch):
        gate = CountingGate()
        client = _FakeClient([RuntimeError("connection reset by peer"), _impact_ok()], gate)
        held_during_sleep: list[bool] = []
        monkeypatch.setattr(impact.time, "sleep", lambda _s: held_during_sleep.append(gate.held))

        result = run_drawing_impact(
            digest_text="DIGEST", findings=[], client=client, call_gate=gate
        )

        assert result.status == "completed"
        assert held_during_sleep == [False]
        assert gate.acquisitions == 2


# ---------------------------------------------------------------------------
# Pipeline wiring: the permit pool reaches the runners as a per-call gate
# ---------------------------------------------------------------------------


def _submission(project_context: str = "") -> BatchSubmission:
    spec = ExtractedSpec(filename="23 00 00.docx", content="HVAC body", word_count=2)
    return BatchSubmission(
        job=BatchJob(batch_id="b1", job_type="review", request_map={}, created_at=0.0),
        cross_check_enabled=True,
        prepared_specs=[spec],
        project_context=project_context,
    )


class TestPipelineWiring:
    def test_batch_stage_helpers_default_to_no_gate(self, monkeypatch):
        seen: dict = {}

        def fake_chunked(specs, existing, **kw):
            seen["call_gate"] = kw.get("call_gate", "missing")
            return ReviewResult(findings=[], cross_check_status="completed")

        monkeypatch.setattr(pl, "run_chunked_cross_check", fake_chunked)
        submission = _submission()
        state = CollectedBatchState(submission=submission, review_result=ReviewResult(findings=[]))

        run_cross_check_for_batch(state, specs=submission.prepared_specs)

        assert seen["call_gate"] is None

        impact_seen: dict = {}

        def fake_impact(**kw):
            impact_seen["call_gate"] = kw.get("call_gate", "missing")
            return DrawingImpactResult(status="completed")

        monkeypatch.setattr(di_pkg, "run_drawing_impact", fake_impact)
        digest_context = wrap_attachment(DIGEST_ATTACHMENT_LABEL, "DIGEST [p.1]")
        state = CollectedBatchState(
            submission=_submission(digest_context), review_result=ReviewResult(findings=[])
        )

        run_drawing_impact_for_batch(state)

        assert impact_seen["call_gate"] is None

    def test_headless_driver_threads_the_pool_without_holding_it(self, monkeypatch):
        gate = CountingGate()
        seen: dict = {}

        def fake_chunked(specs, existing, **kw):
            seen["call_gate"] = kw.get("call_gate")
            seen["held"] = gate.held
            return ReviewResult(findings=[], cross_check_status="completed")

        monkeypatch.setattr(pl, "run_chunked_cross_check", fake_chunked)
        impact_seen: dict = {}

        def fake_impact(**kw):
            impact_seen["call_gate"] = kw.get("call_gate")
            impact_seen["held"] = gate.held
            return DrawingImpactResult(status="completed")

        monkeypatch.setattr(di_pkg, "run_drawing_impact", fake_impact)

        digest_context = wrap_attachment(DIGEST_ATTACHMENT_LABEL, "DIGEST [p.1]")
        submission = _submission(digest_context)
        state = CollectedBatchState(submission=submission, review_result=ReviewResult(findings=[]))
        monkeypatch.setattr(pl, "collect_review_batch_results", lambda _submission, **_kw: state)

        result = pl.run_batch_collection_headless(
            submission,
            cache=VerificationCache(),
            include_drawing_impact=True,
            api_call_semaphore=gate,
        )

        # The pool reaches both runners as their per-call gate ...
        assert seen["call_gate"] is gate
        assert impact_seen["call_gate"] is gate
        # ... and the driver never holds it around a whole pass.
        assert seen["held"] is False
        assert impact_seen["held"] is False
        assert gate.acquisitions == 0
        assert result.cross_check_result is not None
        assert result.drawing_impact_result is not None
