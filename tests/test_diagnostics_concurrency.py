"""Concurrency contracts for the shared in-memory diagnostics report."""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor

from src.orchestration.diagnostics import DiagnosticsReport, _event_data_byte_size


def test_concurrent_logging_and_summary_preserve_caps_and_accounting() -> None:
    """Writers and snapshots share one coherent event/counter state.

    Every round releases all writers and the snapshot reader together.  The
    payload intentionally hits both the per-event cap and secret scrubber,
    while the small cumulative cap continually evicts old events.  This
    exercises the full mutable surface rather than only concurrent appends.
    """

    writer_count = 4
    rounds = 60
    submitted = writer_count * rounds
    report = DiagnosticsReport(
        max_events=0,
        max_event_data_bytes=256,
        max_total_data_bytes=1_024,
    )
    round_start = threading.Barrier(writer_count + 1)
    snapshots: list[dict] = []
    text_snapshots: list[str] = []
    payload = {
        "api_call": True,
        "model": "unit-test-model",
        "input_tokens": 1,
        "output_tokens": 2,
        "api_key": "sk-ant-" + ("x" * 32),
        "payload": "x" * 4_000,
    }

    def write_events(worker_id: int) -> None:
        for round_index in range(rounds):
            round_start.wait()
            report.log(
                "review",
                "info",
                f"worker={worker_id} round={round_index}",
                payload,
            )

    def read_summaries() -> None:
        for round_index in range(rounds):
            round_start.wait()
            snapshots.append(report.summary())
            if round_index % 10 == 0:
                text_snapshots.append(report.to_text())

    with ThreadPoolExecutor(max_workers=writer_count + 1) as pool:
        futures = [
            pool.submit(write_events, worker_id)
            for worker_id in range(writer_count)
        ]
        futures.append(pool.submit(read_summaries))
        for future in futures:
            future.result()

    # Every concurrent summary must describe one atomic point in the event
    # stream: token rollups and retained-event counts cannot come from
    # different versions of the list, and cumulative bytes never exceed cap.
    for snapshot in snapshots:
        assert snapshot["total_input_tokens"] == snapshot["total_events"]
        assert snapshot["total_output_tokens"] == snapshot["total_events"] * 2
        assert snapshot["total_data_bytes"] <= report.max_total_data_bytes
        assert snapshot["events_dropped"] + snapshot["total_events"] <= submitted

    # Text export nests ``summary()`` and then renders the timeline.  The
    # reentrant guard must keep those two views on exactly the same snapshot.
    for text_snapshot in text_snapshots:
        match = re.search(r"^  Events:\s+(\d+)$", text_snapshot, flags=re.MULTILINE)
        assert match is not None
        assert int(match.group(1)) == text_snapshot.count("worker=")

    final = report.summary()
    retained_sizes = [_event_data_byte_size(event.data) for event in report.events]
    assert retained_sizes
    assert len(set(retained_sizes)) == 1
    event_size = retained_sizes[0]
    expected_retained = min(submitted, report.max_total_data_bytes // event_size)

    assert final["total_events"] == expected_retained
    assert final["events_dropped"] == submitted - expected_retained
    assert final["events_dropped"] + final["total_events"] == submitted
    assert final["events_truncated_by_size"] == submitted
    assert final["secrets_redacted"] == submitted
    assert final["total_data_bytes"] == sum(retained_sizes)
    assert final["bytes_dropped"] == final["events_dropped"] * event_size
    assert final["total_input_tokens"] == expected_retained
    assert final["total_output_tokens"] == expected_retained * 2


def test_finish_and_failed_spec_updates_are_idempotent_under_contention() -> None:
    report = DiagnosticsReport()
    start = threading.Barrier(8)

    def update(worker_id: int) -> None:
        start.wait()
        report.record_failed_spec(f"spec-{worker_id % 3}.docx")
        report.finish()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, range(8)))

    summary = report.summary()
    assert sorted(summary["failed_specs"]) == [
        "spec-0.docx",
        "spec-1.docx",
        "spec-2.docx",
    ]
    assert report.ended_at is not None
