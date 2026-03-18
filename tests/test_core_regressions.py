from pathlib import Path

import pytest

from src.code_cycles import CALIFORNIA_2022, CALIFORNIA_2025
from src.extractor import SUPPORTED_EXTENSIONS, ExtractedSpec, extract_text
from src.prompts import get_system_prompt, get_single_spec_user_message
from src.pipeline import _deduplicate_findings, BatchSubmission
from src.reviewer import Finding, _stream_review
from src.cross_checker import run_cross_check
from src.batch import BatchJob, submit_verification_batch
from src import gui
from src.verifier import verify_finding
from src.resume_state import (
    PHASE_REVIEW_POLL,
    PHASE_REVIEW_COLLECT,
    PHASE_VERIFICATION_POLL,
    build_resume_state,
    serialize_batch_job,
    deserialize_batch_job,
)


def test_supported_extensions_docx_only(tmp_path: Path):
    assert SUPPORTED_EXTENSIONS == {".docx"}
    pdf = tmp_path / "sample.pdf"
    pdf.write_text("not a docx")
    with pytest.raises(ValueError):
        extract_text(pdf)


def test_cycle_prompts_change():
    p2022 = get_system_prompt(CALIFORNIA_2022)
    p2025 = get_system_prompt(CALIFORNIA_2025)
    assert "CBC 2022" in p2022
    assert "CBC 2025" in p2025

    msg = get_single_spec_user_message("Body", "file.docx", cycle=CALIFORNIA_2025)
    assert "ASCE 7-22" in msg


def test_dedup_does_not_merge_different_edits():
    f1 = Finding(severity="HIGH", fileName="a.docx", section="1", issue="Same issue", actionType="EDIT", existingText="foo", replacementText="bar", codeReference="CBC", confidence=0.8)
    f2 = Finding(severity="HIGH", fileName="b.docx", section="1", issue="Same issue", actionType="EDIT", existingText="different", replacementText="bar", codeReference="CBC", confidence=0.8)
    deduped = _deduplicate_findings([f1, f2])
    assert len(deduped) == 2


def test_cross_check_skip_status():
    result = run_cross_check([], [])
    assert result.cross_check_status == "skipped"


def test_stream_review_marks_incomplete_when_stop_reason_not_end_turn():
    class _FakeStream:
        def __init__(self):
            self.text_stream = iter(['[{"severity":"HIGH"'])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get_final_message(self):
            usage = type("Usage", (), {"input_tokens": 7, "output_tokens": 11})()
            return type("Resp", (), {"stop_reason": "max_tokens", "usage": usage})()

    class _FakeMessages:
        def stream(self, **_kwargs):
            return _FakeStream()

    class _FakeClient:
        messages = _FakeMessages()

    result = _stream_review(_FakeClient(), "sys", "user")

    assert result.parse_status == "incomplete"
    assert result.stop_reason == "max_tokens"
    assert result.error is not None
    assert "stop_reason: max_tokens" in result.error


def test_batch_state_round_trip_preserves_cycle_cross_check_export_and_specs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_path = tmp_path / "batch_state.json"
    monkeypatch.setattr(gui, "_batch_state_path", lambda: state_path)

    submission = BatchSubmission(
        job=BatchJob(
            batch_id="msgbatch_test_roundtrip",
            job_type="review",
            request_map={"review__spec__0": {"filename": "spec.docx", "index": 0, "type": "review"}},
            created_at=123.0,
        ),
        files_reviewed=["spec.docx"],
        review_request_ids=["review__spec__0"],
        cycle_label="2022",
        cross_check_enabled=True,
        export_mode=True,
        prepared_specs=[
            ExtractedSpec(
                filename="spec.docx",
                content="Section text",
                word_count=2,
                source_path="/tmp/spec.docx",
                source_format="docx",
            )
        ],
    )

    gui.save_batch_state(build_resume_state(phase=PHASE_REVIEW_POLL, submission=submission))
    loaded = gui.load_batch_state()

    assert loaded is not None
    loaded_submission = loaded["submission"]
    loaded_phase = loaded["phase"]
    assert loaded_phase == PHASE_REVIEW_POLL
    assert loaded_submission.cycle_label == "2022"
    assert loaded_submission.cross_check_enabled is True
    assert loaded_submission.export_mode is True
    assert loaded_submission.prepared_specs is not None
    assert len(loaded_submission.prepared_specs) == 1
    assert loaded_submission.prepared_specs[0].filename == "spec.docx"
    assert loaded_submission.prepared_specs[0].content == "Section text"
    assert loaded_submission.prepared_specs[0].word_count == 2


def test_verify_finding_non_end_turn_returns_unverified_with_stop_reason(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class _FakeMessages:
        def create(self, **_kwargs):
            return type("Resp", (), {"stop_reason": "max_tokens", "content": []})()

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr("src.verifier._get_client", lambda: _FakeClient())

    finding = Finding(
        severity="HIGH",
        fileName="spec.docx",
        section="1",
        issue="Issue",
        actionType="EDIT",
        existingText="old",
        replacementText="new",
        codeReference="CBC",
    )

    result = verify_finding(finding)
    assert result.verdict == "UNVERIFIED"
    assert "stop_reason: max_tokens" in result.explanation


def test_verification_batch_job_round_trip():
    job = BatchJob(
        batch_id="msgbatch_verify_roundtrip",
        job_type="verify",
        request_map={"verify__0": {"batch_idx": 0, "finding_idx": 3}},
        created_at=10.0,
    )
    payload = serialize_batch_job(job)
    restored = deserialize_batch_job(payload)
    assert restored.batch_id == job.batch_id
    assert restored.job_type == "verify"
    assert restored.request_map["verify__0"]["finding_idx"] == 3


def test_load_batch_state_legacy_phase_migrates_to_review_poll(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_path = tmp_path / "batch_state.json"
    monkeypatch.setattr(gui, "_batch_state_path", lambda: state_path)
    state_path.write_text(
        """{
  "saved_at": "2026-03-18T00:00:00+00:00",
  "phase": "review",
  "batch_id": "msgbatch_legacy",
  "job_type": "review",
  "request_map": {"review__spec__0": {"filename": "spec.docx", "index": 0, "type": "review"}},
  "created_at": 123.0,
  "files_reviewed": ["spec.docx"],
  "review_request_ids": ["review__spec__0"],
  "leed_alerts": [],
  "placeholder_alerts": [],
  "cross_check_enabled": false,
  "export_mode": false
}""",
        encoding="utf-8",
    )
    loaded = gui.load_batch_state()
    assert loaded is not None
    assert loaded["phase"] == PHASE_REVIEW_POLL
    assert loaded["submission"].job.batch_id == "msgbatch_legacy"


def test_resume_batch_uses_saved_export_mode_not_live_selector(monkeypatch: pytest.MonkeyPatch):
    class _Entry:
        def get(self): return "key"

    class _Selector:
        def __init__(self, value="View in App"):
            self.value = value
        def set(self, value): self.value = value
        def get(self): return self.value

    class _Var:
        def __init__(self): self.value = None
        def set(self, value): self.value = value

    class _Log:
        def log_error(self, _m): pass
        def log_warning(self, _m): pass
        def log(self, *_args, **_kwargs): pass
        def log_step(self, *_args, **_kwargs): pass

    class _Button:
        def set_processing(self): pass
        def configure(self, **_kwargs): pass

    class _Progress:
        def pack(self, **_kwargs): pass
        def set(self, _v): pass
        def configure(self, **_kwargs): pass

    dummy = type("Dummy", (), {})()
    dummy.api_key_entry = _Entry()
    dummy.log = _Log()
    dummy.output_selector = _Selector(value="View in App")
    dummy.cycle_selector = _Selector(value="2022")
    dummy._cross_check_var = _Var()
    dummy.run_button = _Button()
    dummy.progress_bar = _Progress()
    dummy._poll_batch = lambda: None
    dummy._collect_batch_results = lambda: None
    dummy._resume_verification_poll = lambda _loaded: None
    dummy._on_review_complete = lambda _result: None
    dummy._reset_ui = lambda: None
    dummy._on_output_mode_change = lambda _value: None
    dummy.after = lambda *_args, **_kwargs: None

    submission = BatchSubmission(
        job=BatchJob(batch_id="msgbatch_export", job_type="review", request_map={}, created_at=1.0),
        export_mode=True,
        cycle_label="2025",
        cross_check_enabled=False,
    )
    state = {"phase": PHASE_REVIEW_POLL, "submission": submission}
    gui.SpecReviewApp._resume_batch(dummy, state)
    assert dummy._export_mode_for_review is True
    assert dummy.output_selector.get() == "Export Report"


def test_resume_batch_phase_router_calls_expected_handler(monkeypatch: pytest.MonkeyPatch):
    called = {"poll": 0, "collect": 0, "verify": 0}

    class _Entry:
        def get(self): return "key"

    class _Selector:
        def __init__(self): self.value = "View in App"
        def set(self, value): self.value = value
        def get(self): return self.value

    class _Var:
        def set(self, _value): pass

    class _Log:
        def log_error(self, _m): pass
        def log_warning(self, _m): pass
        def log(self, *_args, **_kwargs): pass
        def log_step(self, *_args, **_kwargs): pass

    class _Button:
        def set_processing(self): pass
        def configure(self, **_kwargs): pass

    class _Progress:
        def pack(self, **_kwargs): pass
        def set(self, _v): pass
        def configure(self, **_kwargs): pass

    dummy = type("Dummy", (), {})()
    dummy.api_key_entry = _Entry()
    dummy.log = _Log()
    dummy.output_selector = _Selector()
    dummy.cycle_selector = _Selector()
    dummy._cross_check_var = _Var()
    dummy.run_button = _Button()
    dummy.progress_bar = _Progress()
    dummy._poll_batch = lambda: called.__setitem__("poll", called["poll"] + 1)
    dummy._collect_batch_results = lambda: called.__setitem__("collect", called["collect"] + 1)
    dummy._resume_verification_poll = lambda _loaded: called.__setitem__("verify", called["verify"] + 1)
    dummy._on_review_complete = lambda _result: None
    dummy._reset_ui = lambda: None
    dummy._on_output_mode_change = lambda _value: None
    dummy.after = lambda *_args, **_kwargs: None

    submission = BatchSubmission(
        job=BatchJob(batch_id="msgbatch_router", job_type="review", request_map={}, created_at=1.0),
    )
    gui.SpecReviewApp._resume_batch(dummy, {"phase": PHASE_REVIEW_POLL, "submission": submission})
    gui.SpecReviewApp._resume_batch(dummy, {"phase": PHASE_REVIEW_COLLECT, "submission": submission})
    gui.SpecReviewApp._resume_batch(dummy, {"phase": PHASE_VERIFICATION_POLL, "submission": submission, "review_state": object(), "verification_batch": object()})
    assert called == {"poll": 1, "collect": 1, "verify": 1}


def test_verification_request_map_preserves_sorted_confidence_indices(monkeypatch: pytest.MonkeyPatch):
    class _FakeBatches:
        def create(self, requests):
            assert [r["custom_id"] for r in requests] == ["verify__0", "verify__1"]
            return type("B", (), {"id": "msgbatch_verify_idx"})()

    class _FakeMessages:
        batches = _FakeBatches()

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr("src.batch._get_client", lambda: _FakeClient())

    finding_a = Finding(severity="HIGH", fileName="a", section="1", issue="a", actionType="EDIT", existingText=None, replacementText=None, codeReference=None, confidence=0.9)
    finding_b = Finding(severity="HIGH", fileName="b", section="1", issue="b", actionType="EDIT", existingText=None, replacementText=None, codeReference=None, confidence=0.1)
    job = submit_verification_batch([finding_a, finding_b], build_prompt_fn=lambda _: "prompt")
    # Lower confidence finding_b should be first in batch but still map to original index 1.
    assert job.request_map["verify__0"]["finding_idx"] == 1
    assert job.request_map["verify__1"]["finding_idx"] == 0
