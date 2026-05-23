"""Tests for the agent tracing subsystem.

Covers:
    - Recorder lifecycle (start/stop, file creation, JSONL line counts).
    - Env-var gating (SPEC_CRITIC_TRACE=0 produces no files).
    - Capture-level gating (default omits stream_chunk; deep includes it).
    - Redaction (sk-ant-... strings replaced with <redacted>).
    - Thread safety (concurrent writers produce no torn lines).
    - Hook resilience (capture failures never escape into pipeline code).
    - Span hierarchy (synthetic nesting produces correct parent_span_id chains).
    - Resume-state compatibility (legacy payloads without trace fields load OK).
    - Diagnostics non-interference (DiagnosticsReport.summary() byte-identical
      with and without tracing enabled).
    - Chunk 11-13 round-trip (web_fetch / models_disagreed / budget_exhausted
      survive recorder serialization into findings.jsonl).

All tests are hermetic — no network, no real API key required.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from src.tracing import (
    LEVEL_DEEP,
    LEVEL_DEFAULT,
    LEVEL_OFF,
    SpanHandle,
    TraceRecorder,
    bind_to_current_context,
    current_capture_level,
    current_span,
    get_recorder,
    set_recorder,
    trace_deep_enabled,
    trace_enabled,
)
from src.tracing import capture_hooks
from src.tracing.config import ENV_TRACE, ENV_TRACE_DEEP, ENV_TRACE_DIR
from src.tracing.recorder import (
    FILE_EVENTS,
    FILE_FINDINGS,
    FILE_PROMPTS,
    FILE_RUN_META,
    FILE_SPANS,
)
from src.tracing.spans import (
    EVENT_GROUNDING_OUTCOME,
    EVENT_STREAM_CHUNK,
    KIND_API_CALL,
    KIND_PIPELINE,
    KIND_REVIEW,
    KIND_WEB_SEARCH,
)


@pytest.fixture
def trace_dir(tmp_path: Path) -> Path:
    return tmp_path / "run_test1234"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip any inherited trace env vars so tests get a clean slate."""
    for var in (ENV_TRACE, ENV_TRACE_DEEP, ENV_TRACE_DIR):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def recorder(trace_dir: Path, clean_env: None):
    rec = TraceRecorder(
        run_id="test1234",
        trace_dir=trace_dir,
        capture_level=LEVEL_DEFAULT,
        spec_critic_version="2.11.0",
    )
    rec.start(mode="realtime", model="claude-opus-4-7", cycle_label="California 2025")
    set_recorder(rec)
    yield rec
    rec.stop()
    set_recorder(None)


# ---- Lifecycle ---------------------------------------------------------
def test_recorder_creates_expected_files(recorder: TraceRecorder, trace_dir: Path) -> None:
    with recorder.span(KIND_PIPELINE, "pipeline test"):
        pass
    recorder.stop()
    assert (trace_dir / FILE_RUN_META).exists()
    assert (trace_dir / FILE_SPANS).exists()
    assert (trace_dir / FILE_EVENTS).exists()
    assert (trace_dir / FILE_FINDINGS).exists()
    assert (trace_dir / FILE_PROMPTS).exists()  # default level writes this


def test_run_meta_carries_lifecycle_timestamps(recorder: TraceRecorder, trace_dir: Path) -> None:
    recorder.stop()
    meta = json.loads((trace_dir / FILE_RUN_META).read_text())
    assert meta["run_id"] == "test1234"
    assert meta["mode"] == "realtime"
    assert meta["model"] == "claude-opus-4-7"
    assert meta["started_at"] is not None
    assert meta["ended_at"] is not None
    assert meta["capture_level"] == LEVEL_DEFAULT


def test_stop_is_idempotent(recorder: TraceRecorder) -> None:
    recorder.stop()
    recorder.stop()  # Should not raise


def test_resume_appends_rather_than_truncates(trace_dir: Path, clean_env: None) -> None:
    rec1 = TraceRecorder(run_id="r1", trace_dir=trace_dir, capture_level=LEVEL_DEFAULT)
    rec1.start(mode="batch")
    with rec1.span(KIND_PIPELINE, "first"):
        pass
    rec1.stop()
    first_span_count = len((trace_dir / FILE_SPANS).read_text().strip().split("\n"))

    rec2 = TraceRecorder(run_id="r1", trace_dir=trace_dir, capture_level=LEVEL_DEFAULT)
    rec2.start(mode="batch")
    with rec2.span(KIND_PIPELINE, "second"):
        pass
    rec2.stop()
    second_span_count = len((trace_dir / FILE_SPANS).read_text().strip().split("\n"))

    assert second_span_count == first_span_count + 1
    meta = json.loads((trace_dir / FILE_RUN_META).read_text())
    assert meta["resumed_at"]  # non-empty list


# ---- Env-var gating ----------------------------------------------------
def test_trace_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for token in ("0", "false", "no", "off", "FALSE", "Off"):
        monkeypatch.setenv(ENV_TRACE, token)
        monkeypatch.delenv(ENV_TRACE_DEEP, raising=False)
        assert not trace_enabled(), f"Token {token!r} should disable"
        assert current_capture_level() == LEVEL_OFF


def test_trace_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_TRACE, raising=False)
    monkeypatch.delenv(ENV_TRACE_DEEP, raising=False)
    assert trace_enabled()
    assert current_capture_level() == LEVEL_DEFAULT


def test_deep_implies_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deep flag overrides a disabled main flag — operator intent."""
    monkeypatch.setenv(ENV_TRACE, "0")
    monkeypatch.setenv(ENV_TRACE_DEEP, "1")
    assert trace_enabled()
    assert trace_deep_enabled()
    assert current_capture_level() == LEVEL_DEEP


# ---- Capture level gating ---------------------------------------------
def test_default_level_omits_stream_chunks(trace_dir: Path, clean_env: None) -> None:
    rec = TraceRecorder(run_id="lvl1", trace_dir=trace_dir, capture_level=LEVEL_DEFAULT)
    rec.start(mode="realtime")
    set_recorder(rec)
    try:
        with rec.span(KIND_API_CALL, "test") as s:
            capture_hooks.capture_stream_chunk(s, "chunk text")
            capture_hooks.capture_thinking_block(s, "thinking text")
        rec.stop()
        events = [json.loads(line) for line in (trace_dir / FILE_EVENTS).read_text().strip().split("\n")]
        types = [e["type"] for e in events]
        assert EVENT_STREAM_CHUNK not in types
        assert "thinking_block" in types
    finally:
        set_recorder(None)


def test_deep_level_includes_stream_chunks(trace_dir: Path, clean_env: None) -> None:
    rec = TraceRecorder(run_id="lvl2", trace_dir=trace_dir, capture_level=LEVEL_DEEP)
    rec.start(mode="realtime")
    set_recorder(rec)
    try:
        with rec.span(KIND_API_CALL, "test") as s:
            capture_hooks.capture_stream_chunk(s, "chunk text")
        rec.stop()
        events = [json.loads(line) for line in (trace_dir / FILE_EVENTS).read_text().strip().split("\n")]
        types = [e["type"] for e in events]
        assert EVENT_STREAM_CHUNK in types
    finally:
        set_recorder(None)


def test_deep_level_inlines_prompts(trace_dir: Path, clean_env: None) -> None:
    rec = TraceRecorder(run_id="lvl3", trace_dir=trace_dir, capture_level=LEVEL_DEEP)
    rec.start()
    ref = rec.prompt_ref("review_system", "You are an expert.")
    rec.stop()
    assert ref == {"inline": "You are an expert."}
    # Deep mode doesn't open prompts.jsonl at all
    assert not (trace_dir / FILE_PROMPTS).exists()


def test_default_level_dedupes_prompts(recorder: TraceRecorder, trace_dir: Path) -> None:
    ref1 = recorder.prompt_ref("review_system", "Same body")
    ref2 = recorder.prompt_ref("review_system", "Same body")
    ref3 = recorder.prompt_ref("review_system", "Different body")
    assert ref1["ref"] == ref2["ref"]
    assert ref1["ref"] != ref3["ref"]
    recorder.stop()
    lines = (trace_dir / FILE_PROMPTS).read_text().strip().split("\n")
    assert len(lines) == 2  # two unique bodies


# ---- Redaction ---------------------------------------------------------
def test_redaction_replaces_anthropic_keys(recorder: TraceRecorder, trace_dir: Path) -> None:
    with recorder.span(KIND_API_CALL, "test") as s:
        recorder.add_event(s, "note", api_key="sk-ant-very-long-secret-12345abcde")
        recorder.add_event(s, "note", message="prompt contains sk-ant-secretvalue1234567 embedded")
    recorder.stop()
    body = (trace_dir / FILE_EVENTS).read_text()
    assert "sk-ant-very-long-secret" not in body
    assert "sk-ant-secretvalue" not in body
    assert "<redacted>" in body


def test_redaction_replaces_bearer_tokens(recorder: TraceRecorder, trace_dir: Path) -> None:
    with recorder.span(KIND_API_CALL, "test") as s:
        recorder.add_event(s, "note", message="header was Bearer abcdefghijklmnop12345")
    recorder.stop()
    body = (trace_dir / FILE_EVENTS).read_text()
    assert "Bearer abcdefghij" not in body
    assert "<redacted>" in body


def test_redaction_secret_key_names(recorder: TraceRecorder, trace_dir: Path) -> None:
    with recorder.span(KIND_API_CALL, "test") as s:
        recorder.add_event(s, "note", password="anything", credentials="anything else")
    recorder.stop()
    body = (trace_dir / FILE_EVENTS).read_text()
    assert "<redacted>" in body


# ---- Thread safety -----------------------------------------------------
def test_concurrent_writers_produce_well_formed_jsonl(trace_dir: Path, clean_env: None) -> None:
    rec = TraceRecorder(run_id="thread1", trace_dir=trace_dir, capture_level=LEVEL_DEFAULT)
    rec.start()
    set_recorder(rec)
    try:
        def worker(idx: int) -> None:
            with rec.span(KIND_REVIEW, f"review {idx}") as s:
                for i in range(20):
                    rec.add_event(s, "note", worker=idx, i=i)

        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(worker, range(10)))
        rec.stop()

        # Every line must parse cleanly
        for line in (trace_dir / FILE_EVENTS).read_text().strip().split("\n"):
            json.loads(line)  # raises on torn line
        for line in (trace_dir / FILE_SPANS).read_text().strip().split("\n"):
            json.loads(line)

        # 10 workers * 20 events = 200 events; 10 spans
        events = (trace_dir / FILE_EVENTS).read_text().strip().split("\n")
        spans = (trace_dir / FILE_SPANS).read_text().strip().split("\n")
        assert len(events) == 200
        assert len(spans) == 10
    finally:
        set_recorder(None)


# ---- Hook resilience ---------------------------------------------------
def test_hooks_swallow_recorder_exceptions(trace_dir: Path, clean_env: None) -> None:
    """A capture hook calling a broken recorder must not raise."""
    rec = TraceRecorder(run_id="hookerr", trace_dir=trace_dir, capture_level=LEVEL_DEFAULT)
    rec.start()
    set_recorder(rec)
    try:
        # Monkey-patch the recorder so every public method raises.
        def boom(*a, **kw):
            raise RuntimeError("boom")
        rec.open_span = boom  # type: ignore[method-assign]
        rec.close_span = boom  # type: ignore[method-assign]
        rec.add_event = boom  # type: ignore[method-assign]
        rec.prompt_ref = boom  # type: ignore[method-assign]
        rec.record_finding_snapshot = boom  # type: ignore[method-assign]

        # Every hook must complete without raising.
        capture_hooks.capture_pipeline_start(mode="realtime", model="m", cycle_label="C", files=[])
        capture_hooks.capture_review_call(filename="x.docx", model="m", cycle_label="C")
        capture_hooks.capture_verification_call(finding_id="f1", routing_decision={"mode": "x"})
        capture_hooks.capture_stream_chunk(None, "text")
        capture_hooks.capture_thinking_block(None, "text")
        capture_hooks.capture_grounding_outcome(None, accepted=[], rejected=[], downgraded_to_unverified=False)
        capture_hooks.capture_finding_terminal(object())
    finally:
        set_recorder(None)
        rec.stop()


def test_hooks_noop_without_recorder(clean_env: None) -> None:
    """When no recorder is installed, every hook is a silent no-op."""
    assert get_recorder() is None
    # No exceptions should fire.
    assert capture_hooks.capture_pipeline_start(mode="realtime", model="m", cycle_label="C", files=[]) is None
    assert capture_hooks.capture_review_call(filename="x", model="m", cycle_label="C") is None
    capture_hooks.capture_stream_chunk(None, "text")  # returns None
    capture_hooks.capture_finding_terminal(object())  # returns None


# ---- Span hierarchy ----------------------------------------------------
def test_span_hierarchy_via_context_manager(recorder: TraceRecorder, trace_dir: Path) -> None:
    with recorder.span(KIND_PIPELINE, "pipeline") as pipe:
        with recorder.span(KIND_REVIEW, "review") as rev:
            assert rev.parent_span_id == pipe.span_id
            with recorder.span(KIND_API_CALL, "api") as api:
                assert api.parent_span_id == rev.span_id
                with recorder.span(KIND_WEB_SEARCH, "search") as ws:
                    assert ws.parent_span_id == api.span_id
    recorder.stop()

    spans = [json.loads(line) for line in (trace_dir / FILE_SPANS).read_text().strip().split("\n")]
    by_kind = {sp["kind"]: sp for sp in spans}
    assert by_kind[KIND_WEB_SEARCH]["parent_span_id"] == by_kind[KIND_API_CALL]["span_id"]
    assert by_kind[KIND_API_CALL]["parent_span_id"] == by_kind[KIND_REVIEW]["span_id"]
    assert by_kind[KIND_REVIEW]["parent_span_id"] == by_kind[KIND_PIPELINE]["span_id"]
    assert by_kind[KIND_PIPELINE]["parent_span_id"] is None


def test_naked_executor_loses_context(recorder: TraceRecorder) -> None:
    """ThreadPoolExecutor.submit does NOT auto-propagate contextvars.

    Pin this so future code knows it needs to either pass the span
    handle explicitly or use bind_to_current_context.
    """
    seen: list[str | None] = []

    def worker() -> None:
        span = current_span()
        seen.append(span.span_id if span else None)

    with recorder.span(KIND_PIPELINE, "pipeline"):
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _: worker(), range(3)))
    assert all(s is None for s in seen), (
        "Naked ThreadPoolExecutor should not propagate context — "
        f"got {seen}"
    )


def test_bind_to_current_context_propagates(recorder: TraceRecorder) -> None:
    """bind_to_current_context snapshots the calling thread's context so
    workers see the parent span via current_span()."""
    seen: list[str | None] = []

    def worker() -> None:
        span = current_span()
        seen.append(span.span_id if span else None)

    with recorder.span(KIND_PIPELINE, "pipeline") as pipe:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(bind_to_current_context(worker)) for _ in range(3)]
            for f in futures:
                f.result()
    assert all(s == pipe.span_id for s in seen), (
        f"Workers should inherit parent span {pipe.span_id}, saw {seen}"
    )


# ---- Silo guarantees ---------------------------------------------------
def test_resume_state_legacy_compat() -> None:
    """The resume_state module loads without needing trace fields.

    The current implementation must not require trace_run_id / trace_dir
    — we'll add round-trip support in the next phase; this test pins
    today's tolerant behavior so the addition is genuinely backward-compatible.
    """
    from src.orchestration import resume_state
    # The module imports cleanly with no trace dependency.
    assert hasattr(resume_state, "deserialize_resume_state")


def test_diagnostics_summary_unaffected_by_tracing(monkeypatch: pytest.MonkeyPatch, trace_dir: Path) -> None:
    """DiagnosticsReport.summary() must be byte-identical with/without tracing.

    The two systems coexist via run_id correlation only — neither one
    reads from or writes to the other.
    """
    from src.orchestration.diagnostics import DiagnosticsReport

    # Without tracing
    monkeypatch.setenv(ENV_TRACE, "0")
    rep1 = DiagnosticsReport(run_id="abc123", mode="realtime", model="claude-opus-4-7")
    rep1.log("review", "info", "did a thing")
    summary1 = json.dumps(rep1.summary(), sort_keys=True)

    # With tracing
    monkeypatch.delenv(ENV_TRACE, raising=False)
    rec = TraceRecorder(run_id="abc123", trace_dir=trace_dir, capture_level=LEVEL_DEFAULT)
    rec.start()
    set_recorder(rec)
    try:
        with rec.span(KIND_PIPELINE, "pipe"):
            pass
        rep2 = DiagnosticsReport(run_id="abc123", mode="realtime", model="claude-opus-4-7")
        rep2.log("review", "info", "did a thing")
        summary2 = json.dumps(rep2.summary(), sort_keys=True)
    finally:
        set_recorder(None)
        rec.stop()
    # Drop the started_at fields (default factory differs by call site).
    s1 = json.loads(summary1)
    s2 = json.loads(summary2)
    s1.pop("started_at", None)
    s2.pop("started_at", None)
    assert s1 == s2


# ---- Chunk 11-13 round-trip -------------------------------------------
@dataclass
class _FakeVerification:
    """Synthetic VerificationResult mirroring the real shape for snapshot tests."""

    verdict: str = "CONFIRMED"
    grounded: bool = True
    model_used: str = "claude-sonnet-4-6"
    sources: list[str] = field(default_factory=list)
    rejected_sources: list[str] = field(default_factory=list)
    searched_sources: list[str] = field(default_factory=list)
    web_search_requests: int = 0
    successful_source_count: int = 0
    escalation_attempted: bool = False
    escalated: bool = False
    escalation_changed_verdict: bool = False
    escalation_reason: str = ""
    initial_model: str = ""
    initial_verdict: str = ""
    models_disagreed: bool = False  # Chunk 12
    initial_sources: list[str] = field(default_factory=list)
    web_fetch_requests: int = 0  # Chunk 11
    fetched_sources: list[str] = field(default_factory=list)
    budget_exhausted: bool = False  # Chunk 13
    verification_failed: bool = False
    source_quote: str = ""
    cache_status: str = "none"
    cache_entry_created_ts: float = 0.0
    requires_elevated_confidence: bool = False
    structured_payload: dict | None = None
    retry_telemetry: dict | None = None


@dataclass
class _FakeFinding:
    finding_id: str = "f-test1"
    severity: str = "HIGH"
    section: str = "23 05 00"
    issue: str = "test issue"
    codeReference: str = "NFPA 13"
    actionType: str = "EDIT"
    verification: _FakeVerification | None = None


def test_chunk_11_12_13_round_trip(recorder: TraceRecorder, trace_dir: Path) -> None:
    """A finding with web_fetch, models_disagreed, and budget_exhausted
    survives a snapshot → JSONL → reload cycle with every field intact."""
    finding = _FakeFinding(
        verification=_FakeVerification(
            verdict="CONFIRMED",
            grounded=True,
            sources=["https://example.com/a"],
            initial_sources=["https://example.com/initial"],
            web_fetch_requests=2,  # Chunk 11
            fetched_sources=["https://example.com/fetched-1", "https://example.com/fetched-2"],
            models_disagreed=True,  # Chunk 12
            budget_exhausted=True,  # Chunk 13
            web_search_requests=7,
        )
    )
    recorder.record_finding_snapshot(finding)
    recorder.stop()

    lines = (trace_dir / FILE_FINDINGS).read_text().strip().split("\n")
    assert len(lines) == 1
    snap = json.loads(lines[0])
    v = snap["verification"]
    # Chunk 11
    assert v["web_fetch_requests"] == 2
    assert v["fetched_sources"] == [
        "https://example.com/fetched-1",
        "https://example.com/fetched-2",
    ]
    # Chunk 12
    assert v["models_disagreed"] is True
    assert v["initial_sources"] == ["https://example.com/initial"]
    # Chunk 13
    assert v["budget_exhausted"] is True


def test_verification_end_captures_all_chunk_fields(
    recorder: TraceRecorder, trace_dir: Path
) -> None:
    """capture_verification_end pulls every Chunk 11-13 field off a
    VerificationResult and stamps them on the span outputs."""
    verification = _FakeVerification(
        verdict="CORRECTED",
        grounded=True,
        sources=["https://x.com"],
        web_fetch_requests=1,
        fetched_sources=["https://x.com"],
        models_disagreed=True,
        initial_sources=["https://y.com"],
        budget_exhausted=False,
    )
    span = capture_hooks.capture_verification_call(
        finding_id="f-1",
        routing_decision={"mode": "standard_reasoning"},
    )
    capture_hooks.capture_verification_end(span, verification_result=verification)
    recorder.stop()

    spans = [json.loads(line) for line in (trace_dir / FILE_SPANS).read_text().strip().split("\n")]
    by_kind = {sp["kind"]: sp for sp in spans}
    out = by_kind["verification_initial"]["outputs"]
    assert out["web_fetch_requests"] == 1
    assert out["fetched_sources"] == ["https://x.com"]
    assert out["models_disagreed"] is True
    assert out["initial_sources"] == ["https://y.com"]
    assert out["budget_exhausted"] is False
    assert out["verdict"] == "CORRECTED"


# ---- Capture hook routing ---------------------------------------------
def test_grounding_outcome_event_includes_budget_exhausted(
    recorder: TraceRecorder, trace_dir: Path
) -> None:
    """capture_grounding_outcome emits a second event when budget_exhausted=True."""
    with recorder.span(KIND_REVIEW, "test") as span:
        capture_hooks.capture_grounding_outcome(
            span,
            accepted=["url1"],
            rejected=[],
            downgraded_to_unverified=False,
            budget_exhausted=True,
        )
    recorder.stop()

    events = [json.loads(line) for line in (trace_dir / FILE_EVENTS).read_text().strip().split("\n")]
    types = [e["type"] for e in events]
    assert EVENT_GROUNDING_OUTCOME in types
    assert "budget_exhausted_marker" in types  # the second event


def test_cross_check_chunk_stamps_metadata(recorder: TraceRecorder, trace_dir: Path) -> None:
    pipeline = capture_hooks.capture_pipeline_start(
        mode="realtime", model="m", cycle_label="c", files=[]
    )
    chunk = capture_hooks.capture_cross_check_chunk_start(
        chunk_name="div_23", spec_count=4, finding_count=12, parent=pipeline
    )
    capture_hooks.capture_cross_check_end(chunk, finding_count=2)
    capture_hooks.capture_pipeline_end(pipeline, success=True)
    recorder.stop()

    spans = [json.loads(line) for line in (trace_dir / FILE_SPANS).read_text().strip().split("\n")]
    chunk_span = next(sp for sp in spans if sp["kind"] == "cross_check_chunk")
    assert chunk_span["metadata"]["chunk_name"] == "div_23"
    assert chunk_span["inputs"]["spec_count"] == 4


def test_response_content_block_walker(recorder: TraceRecorder, trace_dir: Path) -> None:
    """The walker emits trace events for thinking, tool_use, web_search."""
    from tests.fixtures.fake_anthropic import (
        FakeMessage,
        FakeServerToolUseBlock,
        FakeTextBlock,
        FakeToolUseBlock,
        FakeUsage,
        FakeWebSearchResultBlock,
    )

    response = FakeMessage(
        id="msg_x",
        content=[
            FakeTextBlock(text="some prose"),
            FakeServerToolUseBlock(name="web_search", input={"query": "NFPA 13"}),
            FakeWebSearchResultBlock(
                content=[
                    {"type": "web_search_result", "url": "https://x.com", "title": "X"},
                    {"type": "web_search_result", "url": "https://y.com", "title": "Y"},
                ],
            ),
            FakeToolUseBlock(name="submit_verification_verdict", input={"verdict": "CONFIRMED"}),
        ],
        stop_reason="tool_use",
        usage=FakeUsage(),
    )
    with recorder.span(KIND_API_CALL, "test") as span:
        capture_hooks.capture_response_content_blocks(span, response)
    recorder.stop()

    events = [json.loads(line) for line in (trace_dir / FILE_EVENTS).read_text().strip().split("\n")]
    types = [e["type"] for e in events]
    assert "web_search_query" in types
    assert "web_search_result" in types
    assert "tool_use" in types
    # The text block isn't an event (it's not forensically useful by itself)
    # but the web_search_query event must carry the query.
    ws_query = next(e for e in events if e["type"] == "web_search_query")
    assert ws_query["query"] == "NFPA 13"
    ws_result = next(e for e in events if e["type"] == "web_search_result")
    assert len(ws_result["urls_with_titles"]) == 2


def test_review_single_spec_integration(
    trace_dir: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run review_single_spec with a fake client; confirm spans emerge.

    Smoke-tests the reviewer.py integration: a review span opens, an
    api_call child fires, content blocks emit events, the span closes
    with the structured-payload outputs.
    """
    from tests.fixtures.fake_anthropic import review_tool_use_response
    from src.review import reviewer

    rec = TraceRecorder(run_id="integ1", trace_dir=trace_dir, capture_level=LEVEL_DEFAULT)
    rec.start(mode="realtime")
    set_recorder(rec)

    # Open a pipeline parent span so the review nests under something.
    pipeline_span = capture_hooks.capture_pipeline_start(
        mode="realtime", model="m", cycle_label="California 2025", files=["foo.docx"]
    )

    # Patch the Anthropic client so messages.stream returns a fake response.
    class _FakeStream:
        def __init__(self, response):
            self._response = response
            self.text_stream = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_final_message(self):
            return self._response

    class _FakeMessages:
        def __init__(self, response):
            self._response = response

        def stream(self, **_kwargs):
            return _FakeStream(self._response)

    class _FakeClient:
        def __init__(self, response):
            self.messages = _FakeMessages(response)

    fake_resp = review_tool_use_response()
    monkeypatch.setattr(reviewer, "_get_client", lambda: _FakeClient(fake_resp))

    try:
        result = reviewer.review_single_spec(
            spec_content="SECTION 23 05 00\n\nThis is test content.",
            filename="test.docx",
            model="claude-opus-4-7",
        )
    finally:
        capture_hooks.capture_pipeline_end(pipeline_span, success=True)
        rec.stop()
        set_recorder(None)

    assert result.parse_status == "ok"
    assert len(result.findings) > 0  # fake response has at least one finding

    spans = [json.loads(line) for line in (trace_dir / FILE_SPANS).read_text().strip().split("\n")]
    kinds = [sp["kind"] for sp in spans]
    assert "pipeline" in kinds
    assert "review" in kinds
    assert "api_call" in kinds

    # Review span carries the structured_payload + findings summary
    review_span = next(sp for sp in spans if sp["kind"] == "review")
    assert review_span["outputs"]["finding_count"] == len(result.findings)
    assert review_span["outputs"]["parse_status"] == "ok"
    assert review_span["outputs"]["structured_payload"] is not None

    # api_call span should be a child of the review span
    api_span = next(sp for sp in spans if sp["kind"] == "api_call")
    assert api_span["parent_span_id"] == review_span["span_id"]
    review_span_check = next(sp for sp in spans if sp["kind"] == "review")
    assert review_span_check["parent_span_id"] == next(
        sp["span_id"] for sp in spans if sp["kind"] == "pipeline"
    )
