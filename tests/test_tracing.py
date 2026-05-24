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


# ---- HTML viewer artifact guards --------------------------------------
def _viewer_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent
        / "src" / "tracing" / "viewer" / "trace_viewer.html"
    )


def test_viewer_artifact_exists_and_nonempty() -> None:
    viewer = _viewer_path()
    assert viewer.exists(), "trace_viewer.html must ship with the package"
    assert viewer.stat().st_size > 5000, "viewer looks truncated"


def test_viewer_references_resolve_to_markup() -> None:
    """Every getElementById target must exist as an id= in the markup.

    Cheap structural guard that catches a JS typo or a renamed element
    without needing a browser/JS runtime in CI.
    """
    import re

    html = _viewer_path().read_text(encoding="utf-8")
    referenced = set(re.findall(r"getElementById\([\"']([^\"']+)[\"']\)", html))
    defined = set(re.findall(r'id="([^"]+)"', html))
    missing = referenced - defined
    assert not missing, f"viewer references undefined element ids: {missing}"


def test_viewer_has_all_four_tabs() -> None:
    html = _viewer_path().read_text(encoding="utf-8")
    for tab in ("finding", "span", "timeline", "grounding"):
        assert f'data-tab="{tab}"' in html, f"viewer missing tab: {tab}"


def test_viewer_status_colors_match_report() -> None:
    """The viewer's STATUS_COLORS must mirror report_exporter's hex map so
    a VERIFIED_CONTESTED finding renders the same purple in both surfaces."""
    from src.output.report_status import ReportStatus
    from src.output.report_exporter import STATUS_SHADING

    html = _viewer_path().read_text(encoding="utf-8")
    # Spot-check the two most identity-bearing statuses.
    contested = STATUS_SHADING[ReportStatus.VERIFIED_CONTESTED]  # 800080
    failed = STATUS_SHADING[ReportStatus.VERIFICATION_FAILED]    # B22222
    assert f"#{contested}".upper() in html.upper()
    assert f"#{failed}".upper() in html.upper()


# ---- CLI -------------------------------------------------------------
def _write_run(root: Path, run_id: str, *, started_at: float, findings: list[dict] | None = None,
               mode: str = "realtime") -> Path:
    d = root / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.json").write_text(json.dumps({
        "run_id": run_id, "mode": mode, "model": "claude-opus-4-7",
        "cycle_label": "California 2025", "files_reviewed": ["a.docx"],
        "started_at": started_at, "ended_at": started_at + 5, "capture_level": "default",
    }))
    (d / "spans.jsonl").write_text(
        json.dumps({"span_id": "s1", "parent_span_id": None, "kind": "pipeline",
                    "name": "p", "status": "ok"}) + "\n")
    (d / "events.jsonl").write_text("")
    lines = "".join(json.dumps(f) + "\n" for f in (findings or []))
    (d / "findings.jsonl").write_text(lines)
    return d


def test_cli_list_and_show(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from src.tracing import cli
    import time as _t
    _write_run(tmp_path, "run_a", started_at=_t.time(), findings=[
        {"finding_id": "f1", "severity": "HIGH", "section": "2.2.A", "issue": "edition drift",
         "verification": {"verdict": "CONFIRMED", "grounded": True, "sources": ["http://x"],
                          "models_disagreed": True, "web_fetch_requests": 2}},
        {"finding_id": "f2", "severity": "MEDIUM", "section": "3.1.B", "issue": "placeholder",
         "verification": {"verdict": "UNVERIFIED", "cache_status": "local_skip"}},
    ])
    assert cli.main(["--trace-dir", str(tmp_path), "list"]) == 0
    out = capsys.readouterr().out
    assert "run_a" in out

    assert cli.main(["--trace-dir", str(tmp_path), "show", "run_a"]) == 0
    out = capsys.readouterr().out
    assert "VERIFIED_CONTESTED" in out  # f1 (models_disagreed)
    assert "LOCALLY_CLASSIFIED" in out  # f2 (local_skip)
    assert "models_disagreed" in out
    assert "fetches=2" in out


def test_cli_show_missing_run(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from src.tracing import cli
    assert cli.main(["--trace-dir", str(tmp_path), "show", "nope"]) == 1


def test_cli_show_resolves_by_embedded_run_id(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """A renamed trace folder still resolves by the run_id in run.json."""
    from src.tracing import cli
    import time as _t
    d = _write_run(tmp_path, "weird_dir_name", started_at=_t.time())
    # Embedded run_id differs from the dir name.
    meta = json.loads((d / "run.json").read_text())
    meta["run_id"] = "the_real_id"
    (d / "run.json").write_text(json.dumps(meta))
    assert cli.main(["--trace-dir", str(tmp_path), "show", "the_real_id"]) == 0


def test_cli_prune_older_than(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from src.tracing import cli
    import time as _t
    now = _t.time()
    _write_run(tmp_path, "fresh", started_at=now - 1 * 86400)
    _write_run(tmp_path, "stale", started_at=now - 50 * 86400)
    assert cli.main(["--trace-dir", str(tmp_path), "prune", "--older-than", "30d", "--yes"]) == 0
    assert (tmp_path / "fresh").exists()
    assert not (tmp_path / "stale").exists()


def test_cli_prune_keep_last(tmp_path: Path) -> None:
    from src.tracing import cli
    import time as _t
    now = _t.time()
    for i in range(4):
        _write_run(tmp_path, f"r{i}", started_at=now - i * 86400)
    assert cli.main(["--trace-dir", str(tmp_path), "prune", "--keep-last", "2", "--yes"]) == 0
    remaining = sorted(d.name for d in tmp_path.iterdir() if d.is_dir())
    assert remaining == ["r0", "r1"]  # two most recent kept


def test_cli_parse_duration() -> None:
    from src.tracing.cli import _parse_duration
    assert _parse_duration("30d") == 30 * 86400
    assert _parse_duration("12h") == 12 * 3600
    assert _parse_duration("90m") == 90 * 60
    assert _parse_duration("7") == 7 * 86400  # bare number → days


# ---- Resume-state trace continuity ------------------------------------
def _minimal_submission(trace_span_id: str = ""):
    from src.orchestration.pipeline import BatchSubmission
    from src.batch.batch import BatchJob
    job = BatchJob(
        batch_id="msgbatch_test123",
        job_type="review",
        request_map={"review__a__0": {"filename": "a.docx", "index": 0, "type": "review"}},
        created_at=1000.0,
    )
    return BatchSubmission(
        job=job,
        files_reviewed=["a.docx"],
        review_request_ids=["review__a__0"],
        trace_span_id=trace_span_id,
    )


def test_resume_state_persists_trace_block(trace_dir: Path, clean_env: None) -> None:
    """build_resume_state includes a trace block when a recorder is active,
    and deserialize_resume_state surfaces it for reattachment."""
    from src.orchestration.resume_state import build_resume_state, deserialize_resume_state, PHASE_REVIEW_POLL

    rec = TraceRecorder(run_id="resume_rt", trace_dir=trace_dir, capture_level=LEVEL_DEFAULT)
    rec.start(mode="batch")
    set_recorder(rec)
    try:
        submission = _minimal_submission(trace_span_id="abc123span")
        state = build_resume_state(phase=PHASE_REVIEW_POLL, submission=submission)
    finally:
        rec.stop()
        set_recorder(None)

    assert state["trace"]["run_id"] == "resume_rt"
    assert state["trace"]["capture_level"] == LEVEL_DEFAULT
    assert str(trace_dir) in state["trace"]["trace_dir"]
    assert state["submission"]["trace_span_id"] == "abc123span"

    # Round-trip back out.
    restored = deserialize_resume_state(state)
    assert restored["trace"]["run_id"] == "resume_rt"
    assert restored["submission"].trace_span_id == "abc123span"


def test_resume_state_no_trace_block_when_recorder_off(clean_env: None) -> None:
    """No recorder installed → no trace block (and no crash)."""
    from src.orchestration.resume_state import build_resume_state, deserialize_resume_state, PHASE_REVIEW_POLL

    set_recorder(None)
    submission = _minimal_submission()
    state = build_resume_state(phase=PHASE_REVIEW_POLL, submission=submission)
    assert "trace" not in state
    # Legacy/no-trace payload deserializes cleanly with no trace key.
    restored = deserialize_resume_state(state)
    assert "trace" not in restored
    assert restored["submission"].trace_span_id == ""


def test_reattach_recorder_appends_to_existing_dir(trace_dir: Path, clean_env: None) -> None:
    """reattach_run_recorder reopens the same dir; a span written after
    reattach lands alongside the original spans (append, not truncate)."""
    from src.tracing.session import reattach_run_recorder as _reattach_recorder, stop_run_recorder as _stop_recorder

    # First session: write one span.
    rec1 = TraceRecorder(run_id="reattach1", trace_dir=trace_dir, capture_level=LEVEL_DEFAULT)
    rec1.start(mode="batch")
    set_recorder(rec1)
    with rec1.span(KIND_PIPELINE, "first session"):
        pass
    rec1.stop()
    set_recorder(None)
    first_count = len((trace_dir / FILE_SPANS).read_text().strip().split("\n"))

    # Reattach (simulating app-restart resume) and write another span.
    rec2 = _reattach_recorder({"run_id": "reattach1", "trace_dir": str(trace_dir), "capture_level": LEVEL_DEFAULT})
    assert rec2 is not None
    assert get_recorder() is rec2
    with rec2.span(KIND_REVIEW, "resumed session"):
        pass
    _stop_recorder(rec2)
    assert get_recorder() is None

    second_count = len((trace_dir / FILE_SPANS).read_text().strip().split("\n"))
    assert second_count == first_count + 1


def test_reattach_recorder_none_when_no_trace_meta(clean_env: None) -> None:
    from src.tracing.session import reattach_run_recorder as _reattach_recorder
    assert _reattach_recorder(None) is None
    assert _reattach_recorder({}) is None
    assert _reattach_recorder({"trace_dir": "/tmp/x"}) is None  # missing run_id


# ---- Batch verification spans -----------------------------------------
def test_batch_verification_span_emitted_for_web_verified(recorder: TraceRecorder, trace_dir: Path) -> None:
    v = _FakeVerification(verdict="CONFIRMED", grounded=True, cache_status="miss",
                          web_fetch_requests=1, models_disagreed=True)
    capture_hooks.capture_batch_verification_span(finding_id="rf-9", verification_result=v)
    recorder.stop()
    spans = [json.loads(line) for line in (trace_dir / FILE_SPANS).read_text().strip().split("\n")]
    vspans = [s for s in spans if s["kind"] == "verification_initial"]
    assert len(vspans) == 1
    assert vspans[0]["metadata"]["finding_id"] == "rf-9"
    assert vspans[0]["metadata"]["source"] == "batch"
    assert vspans[0]["outputs"]["web_fetch_requests"] == 1
    assert vspans[0]["outputs"]["models_disagreed"] is True


def test_batch_verification_span_skips_local_skip_and_cache_hit(recorder: TraceRecorder, trace_dir: Path) -> None:
    """Local-skip / cache-hit results never went through web verification,
    so they don't get a batch verification span (already represented by
    cache_lookup / local_skip events)."""
    capture_hooks.capture_batch_verification_span(
        finding_id="rf-local", verification_result=_FakeVerification(cache_status="local_skip"))
    capture_hooks.capture_batch_verification_span(
        finding_id="rf-cached", verification_result=_FakeVerification(cache_status="hit"))
    recorder.stop()
    spans_path = trace_dir / FILE_SPANS
    spans = [json.loads(line) for line in spans_path.read_text().strip().split("\n")] if spans_path.read_text().strip() else []
    vspans = [s for s in spans if s["kind"] == "verification_initial"]
    assert len(vspans) == 0
