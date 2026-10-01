"""Tracing is optional: a trace failure never ends or strands a run
(plan WP-13, chunk S16).

``start_run_recorder`` / ``reattach_run_recorder`` used to raise straight out
of the GUI workers, before their ``try``: a trace directory that could not be
created left the app stuck in "processing". They now dispose of whatever was
started, warn once, and return ``None``. ``stop_run_recorder`` never raises
and clears the global recorder only when it is still the recorder being
stopped. The deep-trace half of the chunk is here too: a deep trace asks for
summarized thinking, and a thinking block that came back without text is
recorded as not returned.

The GUI workers that call these are covered in ``test_gui_run_credentials.py``.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from src.core import api_config
from src.core.api_config import (
    MODEL_HAIKU_45,
    MODEL_OPUS_48,
    MODEL_OPUS_5,
    MODEL_SONNET_46,
    MODEL_SONNET_5,
    PHASE_REVIEW,
    PHASE_TRIAGE,
    THINKING_DISPLAY_SUMMARIZED,
    thinking_config_for,
)
from src.tracing import capture_hooks
from src.tracing import recorder as recorder_module
from src.tracing import session
from src.tracing.recorder import TraceRecorder, clear_recorder, get_recorder, set_recorder
from src.tracing.spans import EVENT_THINKING_BLOCK


def _writer_threads() -> list[threading.Thread]:
    return [
        t for t in threading.enumerate()
        if t.name.startswith("spec-critic-trace-writer-") and t.is_alive()
    ]


@pytest.fixture(autouse=True)
def _isolated_recorder(monkeypatch, tmp_path):
    monkeypatch.setenv("SPEC_CRITIC_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.delenv("SPEC_CRITIC_TRACE", raising=False)
    monkeypatch.delenv("SPEC_CRITIC_TRACE_DEEP", raising=False)
    set_recorder(None)
    yield
    rec = get_recorder()
    if rec is not None:
        try:
            rec.stop()
        except Exception:
            pass
    set_recorder(None)


def _start(**overrides):
    kwargs = dict(
        run_id="run_test", mode="batch", model="claude-opus-5",
        cycle_label="CA 2025", files=[], module_id="california_k12_mep",
    )
    kwargs.update(overrides)
    return session.start_run_recorder(**kwargs)


# ---------------------------------------------------------------------------
# Startup failures
# ---------------------------------------------------------------------------


class TestStartupNeverRaises:
    def test_an_uncreatable_trace_directory(self, monkeypatch, tmp_path, caplog):
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("a file where the trace root should be")
        monkeypatch.setenv("SPEC_CRITIC_TRACE_DIR", str(blocker))
        warnings: list[str] = []
        before = len(_writer_threads())

        rec = _start(warn=warnings.append)

        assert rec is None
        assert get_recorder() is None
        assert len(_writer_threads()) == before
        assert len(warnings) == 1
        assert warnings[0].startswith("Tracing is off for this run")
        assert "The review continues without a trace." in warnings[0]
        assert sum("Tracing is off" in r.message for r in caplog.records) == 1

    def test_run_json_that_cannot_be_written(self, monkeypatch):
        def fail(self):
            raise PermissionError("run.json is read-only")

        monkeypatch.setattr(TraceRecorder, "_write_run_meta_sync", fail)
        warnings: list[str] = []
        assert _start(warn=warnings.append) is None
        assert get_recorder() is None
        assert warnings and "PermissionError: run.json is read-only" in warnings[0]

    def test_a_writer_thread_that_cannot_start(self, monkeypatch):
        real_thread = threading.Thread

        class NoStart(real_thread):
            def start(self):
                raise RuntimeError("can't start new thread")

        monkeypatch.setattr(recorder_module.threading, "Thread", NoStart)
        warnings: list[str] = []
        assert _start(warn=warnings.append) is None
        assert get_recorder() is None
        assert len(warnings) == 1

    def test_a_failure_after_the_writer_started_disposes_it(self, monkeypatch):
        started: list[TraceRecorder] = []
        real_set = session.set_recorder

        def install_then_fail(rec):
            started.append(rec)
            real_set(rec)
            raise RuntimeError("installed, then failed")

        monkeypatch.setattr(session, "set_recorder", install_then_fail)
        before = len(_writer_threads())
        warnings: list[str] = []

        assert _start(warn=warnings.append) is None

        assert started and started[0].writer_alive is False
        assert len(_writer_threads()) == before
        assert get_recorder() is None
        assert len(warnings) == 1

    def test_a_trace_file_the_writer_cannot_open(self, tmp_path):
        """``run.json`` is writable but a JSONL target is not: the writer
        fails on its own thread, and ``start()`` must hear of it before the
        recorder is installed (found in review)."""
        run_dir = tmp_path / "traces" / "run_test"
        run_dir.mkdir(parents=True)
        (run_dir / "spans.jsonl").mkdir()  # a directory where a file is appended
        warnings: list[str] = []
        before = len(_writer_threads())

        rec = _start(warn=warnings.append)

        assert rec is None
        assert get_recorder() is None
        assert len(_writer_threads()) == before
        assert len(warnings) == 1 and "IsADirectoryError" in warnings[0]
        assert api_config.deep_trace_recording() is False

    def test_a_writer_that_never_opens_its_files_times_out(self, monkeypatch):
        release = threading.Event()
        real_open = TraceRecorder._open_writers

        def wedged(self):
            release.wait(timeout=10)
            return real_open(self)

        monkeypatch.setattr(recorder_module, "_WRITER_START_TIMEOUT_SECONDS", 0.2)
        monkeypatch.setattr(TraceRecorder, "_open_writers", wedged)
        warnings: list[str] = []
        try:
            assert _start(warn=warnings.append) is None
            assert get_recorder() is None
            assert len(warnings) == 1 and "TimeoutError" in warnings[0]
        finally:
            release.set()
        # Once the filesystem answers, the writer reads its shutdown sentinel
        # and exits; nothing lingers.
        for thread in _writer_threads():
            thread.join(timeout=5)
        assert _writer_threads() == []

    def test_a_writer_that_dies_later_stops_taking_events(self, monkeypatch):
        """A writer crash after a good start: nothing more is queued for a
        thread that will never drain it, and a deep trace no longer changes
        requests."""
        monkeypatch.setenv("SPEC_CRITIC_TRACE_DEEP", "1")
        rec = _start()
        try:
            assert rec is not None and rec.is_deep
            assert api_config.deep_trace_recording() is True
            rec._enqueue("no-such-file", {"crash": True})  # the writer skips it
            # Make the writer's next dispatch blow up outside its per-line guard.
            rec._queue.put(("spans.jsonl", object()))
            rec._queue.put(None)  # not a (filename, payload) pair: unpacking fails
            for _ in range(100):
                if not rec.writer_alive:
                    break
                threading.Event().wait(0.02)
            assert rec.writer_alive is False
            before = rec._queue.qsize()
            rec.add_event(None, "note", message="after the crash")
            assert rec._queue.qsize() == before
            assert api_config.deep_trace_recording() is False
        finally:
            session.stop_run_recorder(rec)
        assert get_recorder() is None

    def test_a_failing_warning_sink_is_contained(self, monkeypatch, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        monkeypatch.setenv("SPEC_CRITIC_TRACE_DIR", str(blocker))

        def broken(_message):
            raise RuntimeError("the log widget is gone")

        assert _start(warn=broken) is None

    def test_retention_failure_keeps_the_recorder(self, monkeypatch):
        def fail(**_kwargs):
            raise OSError("prune failed")

        monkeypatch.setattr(session, "apply_startup_retention", fail)
        warnings: list[str] = []
        rec = _start(warn=warnings.append)
        try:
            assert rec is not None and get_recorder() is rec
            assert warnings == []
        finally:
            session.stop_run_recorder(rec)

    def test_control_a_healthy_start_installs_the_recorder(self, tmp_path):
        warnings: list[str] = []
        rec = _start(warn=warnings.append)
        try:
            assert rec is not None
            assert get_recorder() is rec
            assert rec.writer_alive
            assert warnings == []
            assert (tmp_path / "traces" / "run_test" / "run.json").exists()
        finally:
            session.stop_run_recorder(rec)
        assert get_recorder() is None
        assert rec.writer_alive is False

    def test_tracing_disabled_is_a_quiet_none(self, monkeypatch):
        monkeypatch.setenv("SPEC_CRITIC_TRACE", "0")
        warnings: list[str] = []
        assert _start(warn=warnings.append) is None
        assert warnings == []


class TestReattach:
    def test_a_failed_reattach_warns_and_returns_none(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        warnings: list[str] = []
        rec = session.reattach_run_recorder(
            {"run_id": "r1", "trace_dir": str(blocker / "r1")}, warn=warnings.append
        )
        assert rec is None
        assert get_recorder() is None
        assert len(warnings) == 1
        assert "reopen the run's trace" in warnings[0]

    def test_control_a_reattach_appends_to_the_run(self, tmp_path):
        first = _start(run_id="r2")
        session.stop_run_recorder(first)
        rec = session.reattach_run_recorder(
            {"run_id": "r2", "trace_dir": str(first.trace_dir)}
        )
        try:
            assert rec is not None and get_recorder() is rec
            meta = json.loads((first.trace_dir / "run.json").read_text())
            assert len(meta["resumed_at"]) == 1
        finally:
            session.stop_run_recorder(rec)


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


class _BrokenRecorder:
    def stop(self, **_kwargs):
        raise RuntimeError("flush failed")


class TestTeardown:
    def test_stop_never_raises_and_clears_its_own_recorder(self):
        rec = _BrokenRecorder()
        set_recorder(rec)
        session.stop_run_recorder(rec)
        assert get_recorder() is None

    def test_an_old_run_never_clears_a_newer_runs_recorder(self):
        old = _start(run_id="old")
        new = _start(run_id="new")
        try:
            assert get_recorder() is new
            session.stop_run_recorder(old)  # a late teardown from the old run
            assert get_recorder() is new
            assert new.writer_alive
        finally:
            session.stop_run_recorder(new)
        assert get_recorder() is None

    def test_clear_recorder_is_compare_and_clear(self):
        a, b = object(), object()
        set_recorder(a)
        assert clear_recorder(b) is False and get_recorder() is a
        assert clear_recorder(None) is False and get_recorder() is a
        assert clear_recorder(a) is True and get_recorder() is None

    def test_stop_on_a_never_started_recorder_is_safe(self, tmp_path):
        rec = TraceRecorder(run_id="x", trace_dir=tmp_path / "x", capture_level="default")
        rec.stop()
        rec.discard()

    def test_a_discarded_recorder_drops_new_events(self, tmp_path):
        rec = TraceRecorder(run_id="d", trace_dir=tmp_path / "d", capture_level="default")
        rec.start()
        rec.discard()
        assert rec.writer_alive is False
        rec.add_event(None, "note", message="after discard")
        assert rec._queue.qsize() == 0


# ---------------------------------------------------------------------------
# Deep trace: summarized thinking
# ---------------------------------------------------------------------------


class _DeepRecorder:
    is_deep = True


class _DefaultRecorder:
    is_deep = False


class TestThinkingDisplay:
    @pytest.mark.parametrize("model", [MODEL_OPUS_5, MODEL_OPUS_48, MODEL_SONNET_5, MODEL_SONNET_46])
    def test_ordinary_runs_are_unchanged(self, model):
        assert thinking_config_for(model=model, phase=PHASE_REVIEW) == {"type": "adaptive"}
        set_recorder(_DefaultRecorder())
        assert thinking_config_for(model=model, phase=PHASE_REVIEW) == {"type": "adaptive"}

    @pytest.mark.parametrize("model", [MODEL_OPUS_5, MODEL_OPUS_48, MODEL_SONNET_5])
    def test_a_deep_trace_asks_for_summarized_thinking(self, model):
        set_recorder(_DeepRecorder())
        assert thinking_config_for(model=model, phase=PHASE_REVIEW) == {
            "type": "adaptive",
            "display": THINKING_DISPLAY_SUMMARIZED,
        }
        assert THINKING_DISPLAY_SUMMARIZED == "summarized"

    def test_models_without_the_field_never_get_it(self):
        set_recorder(_DeepRecorder())
        # Sonnet 4.6 already returns summarized thinking by default.
        assert thinking_config_for(model=MODEL_SONNET_46, phase=PHASE_REVIEW) == {"type": "adaptive"}
        assert thinking_config_for(model=MODEL_HAIKU_45, phase=PHASE_REVIEW) is None
        assert thinking_config_for(model="claude-unknown-9", phase=PHASE_REVIEW) is None
        assert thinking_config_for(model=MODEL_OPUS_5, phase=PHASE_TRIAGE) is None

    def test_the_capability_is_declared_only_where_documented(self):
        flags = {
            model: api_config.model_capabilities(model).supports_thinking_display
            for model in (MODEL_OPUS_5, MODEL_OPUS_48, MODEL_SONNET_5, MODEL_SONNET_46, MODEL_HAIKU_45)
        }
        assert flags == {
            MODEL_OPUS_5: True, MODEL_OPUS_48: True, MODEL_SONNET_5: True,
            MODEL_SONNET_46: False, MODEL_HAIKU_45: False,
        }

    def test_the_review_request_carries_it_only_in_deep_mode(self):
        from src.review.review_request_builder import ReviewRequestSpec, build_review_request

        spec = ReviewRequestSpec(
            spec_content="PART 1 GENERAL", filename="230500.docx", model=MODEL_OPUS_5,
        )
        ordinary = build_review_request(spec).params
        set_recorder(_DeepRecorder())
        deep = build_review_request(spec).params
        assert ordinary["thinking"] == {"type": "adaptive"}
        assert deep["thinking"] == {"type": "adaptive", "display": "summarized"}
        ordinary_rest = {k: v for k, v in ordinary.items() if k != "thinking"}
        deep_rest = {k: v for k, v in deep.items() if k != "thinking"}
        assert json.dumps(ordinary_rest, sort_keys=True, default=str) == json.dumps(
            deep_rest, sort_keys=True, default=str
        )

    def test_strict_structured_verification_still_omits_thinking(self):
        from src.core.code_cycles import DEFAULT_CYCLE
        from src.review.reviewer import Finding
        from src.verification.verification_modes import VerificationMode
        from src.verification.verification_routing import (
            build_verification_request,
            select_routing,
        )

        finding = Finding(
            severity="GRIPES", fileName="a.docx", section="1.01",
            issue="Typo in a heading.", actionType="REPORT_ONLY",
            existingText="", replacementText="", codeReference="", confidence=0.5,
        )
        strict = select_routing(finding, cycle=DEFAULT_CYCLE, local_skip=False)
        assert strict.mode is VerificationMode.STRICT_STRUCTURED
        standard = select_routing(
            Finding(
                severity="HIGH", fileName="a.docx", section="2.1",
                issue="The cited NFPA 13 edition is not the adopted one.",
                actionType="REPORT_ONLY", existingText="", replacementText="",
                codeReference="NFPA 13", confidence=0.8,
            ),
            cycle=DEFAULT_CYCLE,
        )
        set_recorder(_DeepRecorder())
        strict_params = build_verification_request(strict, prompt="p", system_prompt="s").params
        standard_params = build_verification_request(standard, prompt="p", system_prompt="s").params
        assert "thinking" not in strict_params
        assert standard_params["thinking"] == {"type": "adaptive", "display": "summarized"}


class _Handle:
    span_id = "span_1"


class _CapturingRecorder:
    is_deep = True

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def add_event(self, handle, type, **fields):
        self.events.append((type, fields))


class TestThinkingBlocksInTheTrace:
    def test_a_block_without_text_is_recorded_as_not_returned(self):
        rec = _CapturingRecorder()
        set_recorder(rec)
        capture_hooks.capture_response_content_blocks(
            _Handle(), {"content": [{"type": "thinking", "thinking": "", "signature": "sig"}]}
        )
        assert rec.events == [
            (EVENT_THINKING_BLOCK, {"returned": False, "note": capture_hooks.THINKING_NOT_RETURNED_NOTE})
        ]
        assert "text" not in rec.events[0][1]

    def test_a_summarized_block_is_recorded_with_its_text(self):
        rec = _CapturingRecorder()
        set_recorder(rec)
        capture_hooks.capture_response_content_blocks(
            _Handle(), {"content": [{"type": "thinking", "thinking": "Checked 2.1 first."}]}
        )
        assert rec.events == [
            (EVENT_THINKING_BLOCK, {"text": "Checked 2.1 first.", "returned": True})
        ]

    def test_the_viewer_labels_a_block_not_returned(self):
        viewer = Path(capture_hooks.__file__).parent / "viewer" / "trace_viewer.html"
        text = viewer.read_text(encoding="utf-8")
        assert 'ev.returned === false ? "(thinking not returned' in text
