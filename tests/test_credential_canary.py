"""A run's key reaches the API and nowhere else (plan WP-13, chunk S16).

One review runs end to end under a bound credential holding a canary key,
with a deep trace recording: preparation, the real-time review, verification,
collection, a batch submission whose saved record is written, and every
export (Word report, HTML report with Ask AI, edit sidecar, diagnostics).
The fake SDK client records the key it was built with — that is the
positive control that the credential really carried the run — and then
every artifact is scanned for the canary: the process environment, the
saved batch record, the verification cache, the trace directory, the
reports (the ``.docx`` unzipped), and the diagnostics summary.

Hermetic: the SDK class is replaced, counts are scripted, and every file
lives under ``tmp_path``.
"""
from __future__ import annotations

import json
import os
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.api_config import REVIEW_MODEL_DEFAULT
from src.core.credentials import ApiCredential, run_with_credential
from src.modules import DEFAULT_MODULE
from src.orchestration.batch_resume import PendingBatch, save_pending_batch
from src.orchestration.diagnostics import DiagnosticsReport
from src.orchestration.pipeline import run_batch_collection_headless, start_batch_review
from src.output.edit_sidecar import write_edit_instructions_sidecar
from src.output.html_report_exporter import write_html_report
from src.output.report_exporter import export_report
from src.review import reviewer
from src.tracing import session
from src.tracing.recorder import get_recorder, set_recorder
from tests.fixtures import spec_docx as fx
from tests.fixtures.fake_anthropic import (
    review_tool_use_response,
    sample_review_findings_payload,
    verification_tool_use_response,
)

# Deliberately not shaped like a real key: the trace and diagnostics
# redactors scrub ``sk-ant-…`` strings, and this test is about the credential
# never reaching an artifact at all, not about the redactor catching it.
CANARY = "CANARY-api-credential-must-never-be-written-4444"


class _Stream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        return iter(())

    def get_final_message(self):
        return self._message

    def __iter__(self):
        return iter(())


class _CanarySDK:
    """Stands in for ``anthropic.Anthropic``; records the key it was given."""

    keys: list[str] = []

    def __init__(self, *, api_key: str):
        _CanarySDK.keys.append(api_key)
        self.messages = SimpleNamespace(
            stream=self._stream,
            count_tokens=lambda **_kw: SimpleNamespace(input_tokens=2_000),
            batches=SimpleNamespace(create=lambda **_kw: SimpleNamespace(id="msgbatch_canary")),
        )

    def with_options(self, **_kwargs):
        return self

    @staticmethod
    def _stream(**params):
        tools = {tool.get("name") for tool in params.get("tools") or [] if isinstance(tool, dict)}
        if "submit_review_findings" in tools:
            payload = sample_review_findings_payload()
            payload["findings"][0]["fileName"] = "230500.docx"
            return _Stream(review_tool_use_response(payload=payload))
        return _Stream(verification_tool_use_response())


def _all_text(path: Path) -> str:
    if path.suffix == ".docx":
        with zipfile.ZipFile(path) as archive:
            return "\n".join(
                archive.read(name).decode("utf-8", errors="replace")
                for name in archive.namelist()
            )
    return path.read_bytes().decode("utf-8", errors="replace")


@pytest.fixture
def canary_run(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("SPEC_CRITIC_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("SPEC_CRITIC_TRACE_DEEP", "1")
    monkeypatch.setenv("SPEC_CRITIC_PENDING_BATCH_PATH", str(tmp_path / "pending.json"))
    monkeypatch.setenv("SPEC_CRITIC_CACHE_PATH", str(tmp_path / "cache.json"))
    monkeypatch.setattr(reviewer, "Anthropic", _CanarySDK)
    # The real-time oversize gate also sizes the repair shape, which has no
    # cached API estimate; the local tokenizer's rank file is not available
    # offline, so stub the gate's count (the test_realtime_review convention).
    from src.review import realtime_review

    monkeypatch.setattr(
        realtime_review, "review_extended_output_count",
        lambda request_spec: len(request_spec.spec_content) // 4,
    )
    _CanarySDK.keys = []
    set_recorder(None)
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    spec = fx.save_docx(fx.build_clean_three_part(), spec_dir, "230500.docx")
    out = tmp_path / "out"
    out.mkdir()

    credential = ApiCredential(CANARY, source="gui")
    diag = DiagnosticsReport(mode="realtime")
    ran: dict = {}

    def flow():
        # On a worker thread bound to the run's credential, as the GUI runs
        # it: a submission leaves its pipeline span open for collection,
        # and a span on the test's own thread would leak into later tests.
        recorder = session.start_run_recorder(
            run_id=diag.run_id, mode="realtime", model=REVIEW_MODEL_DEFAULT,
            cycle_label=DEFAULT_MODULE.cycle.label, files=[spec],
        )
        ran["recorder"] = recorder
        assert recorder is not None and recorder.is_deep
        try:
            submission = start_batch_review(
                input_dir=spec_dir, files=[spec], model=REVIEW_MODEL_DEFAULT,
                module=DEFAULT_MODULE, diagnostics=diag, review_transport="realtime",
            )
            ran["result"] = run_batch_collection_headless(submission, diagnostics=diag)
            batch_submission = start_batch_review(
                input_dir=spec_dir, files=[spec], model=REVIEW_MODEL_DEFAULT,
                module=DEFAULT_MODULE, review_transport="batch",
            )
            ran["saved"] = save_pending_batch(
                PendingBatch.from_submission(
                    batch_submission, input_dir=str(spec_dir), files=[str(spec)],
                    run_id=diag.run_id, app_version="test",
                )
            )
        except BaseException as exc:  # surfaced on the test thread below
            ran["error"] = exc
        finally:
            session.stop_run_recorder(recorder)

    worker = threading.Thread(target=run_with_credential(credential, flow))
    worker.start()
    worker.join(timeout=60)
    assert not worker.is_alive()
    if "error" in ran:
        raise ran["error"]
    assert ran["saved"]
    result, recorder = ran["result"], ran["recorder"]

    report = out / "report.docx"
    export_report(result, report)
    write_edit_instructions_sidecar(result, report)
    write_html_report(result, out / "report.html", include_chat=True)
    (out / "diagnostics.json").write_text(
        json.dumps(
            {"summary": diag.summary(), "events": [vars(e) for e in diag.events]},
            default=str,
        ),
        encoding="utf-8",
    )
    (out / "diagnostics.txt").write_text(diag.to_text(), encoding="utf-8")
    yield SimpleNamespace(tmp=tmp_path, result=result, diag=diag, trace_dir=recorder.trace_dir)
    set_recorder(None)


def test_the_run_used_the_bound_key(canary_run):
    assert _CanarySDK.keys and set(_CanarySDK.keys) == {CANARY}
    assert canary_run.result.review_result.findings, "precondition: the review produced a finding"
    assert get_recorder() is None


def test_the_key_is_in_no_artifact(canary_run):
    files = [p for p in canary_run.tmp.rglob("*") if p.is_file()]
    names = {p.relative_to(canary_run.tmp).as_posix() for p in files}
    # Precondition: every surface the acceptance names was actually written.
    for expected in (
        "pending.json", "out/report.docx", "out/report.edits.json",
        "out/report.html", "out/diagnostics.json",
    ):
        assert expected in names, f"precondition: {expected} was not written"
    assert any(n.startswith("traces/") and n.endswith("run.json") for n in names)
    assert any(n.startswith("traces/") and n.endswith("spans.jsonl") for n in names)
    leaked = sorted(
        p.relative_to(canary_run.tmp).as_posix() for p in files if CANARY in _all_text(p)
    )
    assert leaked == []


def test_the_key_is_not_in_the_environment(canary_run):
    assert all(CANARY not in value for value in os.environ.values())
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_the_key_is_not_in_the_diagnostics(canary_run):
    assert CANARY not in json.dumps(canary_run.diag.summary(), default=str)
    assert CANARY not in canary_run.diag.to_text()
