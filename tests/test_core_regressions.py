from pathlib import Path

import pytest

from src.code_cycles import CALIFORNIA_2022, CALIFORNIA_2025
from src.extractor import SUPPORTED_EXTENSIONS, ExtractedSpec, extract_text
from src.prompts import get_system_prompt, get_single_spec_user_message
from src.pipeline import _deduplicate_findings, BatchSubmission
from src.reviewer import Finding, _stream_review
from src.cross_checker import run_cross_check
from src.batch import BatchJob
from src import gui
from src.verifier import verify_finding


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

    gui.save_batch_state(submission, phase="review")
    loaded = gui.load_batch_state()

    assert loaded is not None
    loaded_submission, loaded_phase = loaded
    assert loaded_phase == "review"
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
