"""Program-wide API permits cover verification triage and batch fallback."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from src.orchestration import pipeline as pipeline
from src.review.reviewer import Finding
from src.verification import triage
from tests.fixtures.fake_anthropic import FakeMessage, FakeToolUseBlock


class _TrackingSemaphore:
    def __init__(self, value: int = 1) -> None:
        self._semaphore = threading.BoundedSemaphore(value)
        self._local = threading.local()
        self._lock = threading.Lock()
        self.entries = 0

    def __enter__(self):
        self._semaphore.acquire()
        self._local.held = True
        with self._lock:
            self.entries += 1
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self._local.held = False
        self._semaphore.release()

    def held_by_current_thread(self) -> bool:
        return bool(getattr(self._local, "held", False))


def _finding(index: int) -> Finding:
    return Finding(
        severity="MEDIUM",
        fileName="23 22 00 - Steam.docx",
        section=f"2.{index}",
        issue=f"Finding {index}: pressure rating lookup",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        confidence=0.5,
        codeReference="",
    )


def test_haiku_triage_acquires_one_permit_per_remote_chunk(monkeypatch):
    permits = _TrackingSemaphore()
    call_count = 0

    class Messages:
        def create(self, **_kwargs):
            nonlocal call_count
            assert permits.held_by_current_thread()
            index = call_count
            call_count += 1
            return FakeMessage(
                content=[
                    FakeToolUseBlock(
                        name=triage.TRIAGE_TOOL_NAME,
                        input={
                            "classifications": [
                                {"index": index, "classification": "web_required"}
                            ]
                        },
                    )
                ],
                stop_reason="tool_use",
            )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        triage,
        "_get_client",
        lambda: SimpleNamespace(messages=Messages()),
    )

    classifications = triage.classify_findings_with_haiku(
        [_finding(0), _finding(1)],
        batch_size=1,
        api_call_semaphore=permits,
    )

    assert classifications == {0: "web_required", 1: "web_required"}
    assert call_count == 2
    assert permits.entries == 2


def test_batch_verification_pipeline_forwards_same_permit(monkeypatch):
    permit = object()
    finding = _finding(0)
    job = SimpleNamespace(batch_id="verification-batch", submitted_findings=None)
    seen: list[object] = []

    def fake_start(findings, **kwargs):
        assert findings == [finding]
        seen.append(kwargs["api_call_semaphore"])
        return job

    def fake_collect(got_job, findings, **kwargs):
        assert got_job is job
        assert findings == [finding]
        seen.append(kwargs["api_call_semaphore"])
        return findings

    monkeypatch.setattr(pipeline, "start_batch_verification", fake_start)
    monkeypatch.setattr(
        pipeline,
        "collect_batch_verification_results",
        fake_collect,
    )

    pipeline.verify_findings_for_run(
        [finding],
        transport="batch",
        api_call_semaphore=permit,
    )

    assert seen == [permit, permit]
