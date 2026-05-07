"""Pure-logic tests for the modules extracted from ``src/gui.py``.

These cover the parts of the refactor that don't need a real Tk root —
path helpers, the API key loader, drop-payload parsing, supported-extension
filtering, the verification-resume validator, and the age formatter.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src import app_paths, api_key_store, batch_state_store
from src.batch import BatchJob
from src.batch_controller import (
    format_batch_age,
    is_valid_verification_resume_state,
)
from src.file_selection_controller import (
    filter_supported_specs,
    is_supported_spec,
    parse_dropped_paths,
)
from src.resume_state import PHASE_REVIEW_POLL, build_resume_state
from src.context_controller import extract_context_attachments


# ---------------------------------------------------------------------------
# app_paths
# ---------------------------------------------------------------------------


def test_app_paths_returns_existing_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(app_paths, "user_config_dir", lambda *a, **k: str(tmp_path / "cfg"))
    monkeypatch.setattr(app_paths, "user_state_dir", lambda *a, **k: str(tmp_path / "state"))
    cfg = app_paths.app_config_dir()
    state = app_paths.app_state_dir()
    assert cfg.is_dir()
    assert state.is_dir()


def test_api_key_paths_returns_priority_order(monkeypatch, tmp_path):
    monkeypatch.setattr(app_paths, "user_config_dir", lambda *a, **k: str(tmp_path / "cfg"))
    monkeypatch.setattr(app_paths, "executable_dir", lambda: tmp_path / "exe")
    paths = app_paths.api_key_paths()
    assert len(paths) == 2
    assert "cfg" in str(paths[0])
    assert "exe" in str(paths[1])


# ---------------------------------------------------------------------------
# api_key_store
# ---------------------------------------------------------------------------


def test_load_api_key_returns_empty_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(api_key_store, "api_key_paths", lambda: [tmp_path / "missing.txt"])
    assert api_key_store.load_api_key_from_file() == ""


def test_load_api_key_prefers_first_existing(monkeypatch, tmp_path):
    primary = tmp_path / "primary.txt"
    fallback = tmp_path / "fallback.txt"
    primary.write_text("primary-key\n")
    fallback.write_text("fallback-key\n")
    monkeypatch.setattr(api_key_store, "api_key_paths", lambda: [primary, fallback])
    assert api_key_store.load_api_key_from_file() == "primary-key"


def test_load_api_key_falls_back_to_second(monkeypatch, tmp_path):
    primary = tmp_path / "primary.txt"
    fallback = tmp_path / "fallback.txt"
    fallback.write_text("fallback-key\n")
    monkeypatch.setattr(api_key_store, "api_key_paths", lambda: [primary, fallback])
    assert api_key_store.load_api_key_from_file() == "fallback-key"


# ---------------------------------------------------------------------------
# batch_state_store
# ---------------------------------------------------------------------------


def _make_minimal_submission(batch_id="msgbatch_test_state"):
    from src.pipeline import BatchSubmission

    return BatchSubmission(
        job=BatchJob(
            batch_id=batch_id,
            job_type="review",
            request_map={"review__spec__0": {"filename": "spec.docx", "index": 0, "type": "review"}},
            created_at=1.0,
        ),
        files_reviewed=["spec.docx"],
        review_request_ids=["review__spec__0"],
        cycle_label="2025",
        cross_check_enabled=False,
    )


def test_batch_state_round_trip(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(batch_state_store, "_batch_state_path", lambda: state_path)
    submission = _make_minimal_submission()
    batch_state_store.save_batch_state(build_resume_state(phase=PHASE_REVIEW_POLL, submission=submission))
    loaded = batch_state_store.load_batch_state()
    assert loaded is not None
    assert loaded["submission"].job.batch_id == "msgbatch_test_state"


def test_batch_state_returns_none_for_corrupt_json(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{not valid json")
    monkeypatch.setattr(batch_state_store, "_batch_state_path", lambda: state_path)
    assert batch_state_store.load_batch_state() is None
    # corrupt JSON should also be deleted
    assert not state_path.exists()


def test_batch_state_returns_none_for_invalid_batch_id(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(batch_state_store, "_batch_state_path", lambda: state_path)
    submission = _make_minimal_submission(batch_id="not_a_batch_id")
    batch_state_store.save_batch_state(build_resume_state(phase=PHASE_REVIEW_POLL, submission=submission))
    # The saved file is structurally valid but the batch id doesn't start with msgbatch_
    assert batch_state_store.load_batch_state() is None
    assert not state_path.exists()


def test_batch_state_delete_is_safe_when_missing(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(batch_state_store, "_batch_state_path", lambda: state_path)
    # Should not raise even if the file doesn't exist
    batch_state_store.delete_batch_state()


# ---------------------------------------------------------------------------
# file_selection_controller
# ---------------------------------------------------------------------------


def test_is_supported_spec_only_docx():
    assert is_supported_spec(Path("spec.docx"))
    assert is_supported_spec(Path("spec.DOCX"))
    assert not is_supported_spec(Path("spec.pdf"))
    assert not is_supported_spec(Path("spec.txt"))


def test_filter_supported_specs_drops_unsupported_in_order():
    paths = [Path("a.docx"), Path("b.pdf"), Path("c.docx"), Path("d.txt")]
    assert filter_supported_specs(paths) == [Path("a.docx"), Path("c.docx")]


class _FakeTk:
    """Minimal stand-in for ``self.tk`` exposing only ``splitlist``."""
    class _Inner:
        def splitlist(self, payload):
            # Tcl-style splitlist for brace-quoted paths
            import shlex
            return shlex.split(payload.replace("{", '"').replace("}", '"'))
    tk = _Inner()


def test_parse_dropped_paths_handles_braces_and_spaces():
    payload = "{/tmp/file with space.docx} /tmp/other.docx"
    paths = parse_dropped_paths(_FakeTk(), payload)
    names = [p.name for p in paths]
    assert "file with space.docx" in names
    assert "other.docx" in names


def test_parse_dropped_paths_empty_returns_empty_list():
    assert parse_dropped_paths(_FakeTk(), "") == []


# ---------------------------------------------------------------------------
# context_controller
# ---------------------------------------------------------------------------


def test_extract_context_attachments_collects_errors_for_bad_files(tmp_path):
    bad = tmp_path / "missing.docx"
    combined, errors = extract_context_attachments([bad])
    assert combined == ""
    assert len(errors) == 1
    assert "missing.docx" in errors[0]


# ---------------------------------------------------------------------------
# batch_controller pure helpers
# ---------------------------------------------------------------------------


def test_is_valid_verification_resume_state_requires_review_state():
    submission = _make_minimal_submission()
    verification_batch = BatchJob(
        batch_id="msgbatch_verify",
        job_type="verification",
        request_map={"v__0": {"index": 0}},
        created_at=1.0,
    )
    assert is_valid_verification_resume_state(
        {"review_state": object(), "verification_batch": verification_batch}
    )
    assert not is_valid_verification_resume_state({"verification_batch": verification_batch})
    assert not is_valid_verification_resume_state({"review_state": object()})


def test_is_valid_verification_resume_state_rejects_malformed_batch_id():
    review_state = object()
    bad_batch = BatchJob(
        batch_id="not_msgbatch_",
        job_type="verification",
        request_map={"v__0": {"index": 0}},
        created_at=1.0,
    )
    assert not is_valid_verification_resume_state(
        {"review_state": review_state, "verification_batch": bad_batch}
    )


def test_is_valid_verification_resume_state_rejects_empty_request_map():
    review_state = object()
    empty_map_batch = BatchJob(
        batch_id="msgbatch_xyz",
        job_type="verification",
        request_map={},
        created_at=1.0,
    )
    assert not is_valid_verification_resume_state(
        {"review_state": review_state, "verification_batch": empty_map_batch}
    )


def test_format_batch_age_buckets():
    import time

    now = time.time()
    assert "minutes ago" in format_batch_age(now - 600)
    assert "hours ago" in format_batch_age(now - 7200)
    assert "days ago" in format_batch_age(now - 86400 * 3)
    # Bad input falls back to "unknown time"
    assert format_batch_age("not-a-number") == "unknown time"
