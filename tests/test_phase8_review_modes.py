"""Phase 8 tests — review modes, expanded AEC scope, chunked cross-check.

Plan section 12.1 / 12.2 / 12.3.
"""
from __future__ import annotations

import pytest

from src.code_cycles import CALIFORNIA_2025, DEFAULT_CYCLE
from src.cross_checker import (
    _assign_chunk,
    _group_specs_by_chunk,
    run_chunked_cross_check,
)
from src.extractor import ExtractedSpec
from src.prompts import get_single_spec_user_message, get_system_prompt
from src.review_modes import (
    DEFAULT_REVIEW_MODE,
    REVIEW_MODE_PROFILES,
    ReviewMode,
    coerce_review_mode,
)
from src.reviewer import Finding, ReviewResult


# ---------------------------------------------------------------------------
# 12.1 — review-mode coercion + profile metadata
# ---------------------------------------------------------------------------


def test_coerce_review_mode_accepts_aliases():
    assert coerce_review_mode("strict") is ReviewMode.STRICT
    assert coerce_review_mode("Comprehensive") is ReviewMode.COMPREHENSIVE
    assert coerce_review_mode("safe-edit") is ReviewMode.SAFE_EDIT
    assert coerce_review_mode("safe edit") is ReviewMode.SAFE_EDIT
    assert coerce_review_mode("") is DEFAULT_REVIEW_MODE
    assert coerce_review_mode(None) is DEFAULT_REVIEW_MODE
    assert coerce_review_mode("nonsense") is DEFAULT_REVIEW_MODE
    # Passing the enum through is a no-op.
    assert coerce_review_mode(ReviewMode.STRICT) is ReviewMode.STRICT


def test_default_mode_is_comprehensive():
    # The GUI exposes Comprehensive as the default — confirm the constant
    # matches so the prompt and selector stay in sync.
    assert DEFAULT_REVIEW_MODE is ReviewMode.COMPREHENSIVE
    assert REVIEW_MODE_PROFILES[ReviewMode.COMPREHENSIVE].label.lower().startswith("comprehensive")


# ---------------------------------------------------------------------------
# 12.1 / 12.2 — system prompt is mode-aware and adds expanded AEC scope
# ---------------------------------------------------------------------------


def test_strict_prompt_contains_only_strict_categories():
    prompt = get_system_prompt(CALIFORNIA_2025, mode=ReviewMode.STRICT)
    assert "STRICT" in prompt
    # Comprehensive-only categories must not be present in strict mode.
    assert "TAB" not in prompt
    assert "commissioning" not in prompt.lower() or "tab" not in prompt.lower()
    assert "DSA / HCAI" not in prompt
    assert "Sprinkler / hydraulic" not in prompt


def test_comprehensive_prompt_includes_expanded_aec_scope():
    prompt = get_system_prompt(CALIFORNIA_2025, mode=ReviewMode.COMPREHENSIVE)
    assert "COMPREHENSIVE" in prompt
    # Plan 12.2: explicit AEC categories must surface in the comprehensive prompt.
    expected_phrases = [
        "Constructability",
        "TAB",
        "commissioning",
        "Equipment schedule",
        "Division 01",
        "Warranty",
        "basis-of-design",
        "Controls sequence",
        "DSA / HCAI",
        "Fire and smoke damper",
        "Seismic restraint",
        "Sprinkler / hydraulic",
        "Pipe / duct material",
        "Submittal and O&M",
    ]
    for phrase in expected_phrases:
        assert phrase in prompt, f"comprehensive prompt missing '{phrase}'"


def test_safe_edit_prompt_emphasizes_anchored_edits():
    prompt = get_system_prompt(CALIFORNIA_2025, mode=ReviewMode.SAFE_EDIT)
    assert "SAFE_EDIT" in prompt
    assert "verbatim" in prompt
    assert "anchor" in prompt.lower()


def test_user_message_includes_mode_reminder():
    for mode in (ReviewMode.STRICT, ReviewMode.COMPREHENSIVE, ReviewMode.SAFE_EDIT):
        msg = get_single_spec_user_message(
            "Body", "23 09 23.docx", cycle=CALIFORNIA_2025, mode=mode
        )
        assert "Mode reminder" in msg
        # The mode label appears in the reminder so the model can disambiguate.
        assert mode.value.upper() in msg


def test_default_get_system_prompt_matches_default_mode():
    # Calling without ``mode`` should produce the same prompt as the
    # explicit default. This protects callers that haven't been migrated.
    default_prompt = get_system_prompt(CALIFORNIA_2025)
    explicit = get_system_prompt(CALIFORNIA_2025, mode=DEFAULT_REVIEW_MODE)
    assert default_prompt == explicit


# ---------------------------------------------------------------------------
# 12.3 — chunked cross-check by CSI division
# ---------------------------------------------------------------------------


def _spec(filename: str, body: str = "x" * 200) -> ExtractedSpec:
    return ExtractedSpec(
        filename=filename, content=body, word_count=10,
        source_path=f"/tmp/{filename}", source_format="docx",
    )


def test_assign_chunk_uses_csi_prefix():
    assert _assign_chunk("21 13 13 Wet-Pipe Sprinklers.docx") == "div_21"
    assert _assign_chunk("22 11 16 Domestic Water Piping.docx") == "div_22"
    assert _assign_chunk("23 05 00 Common HVAC Requirements.docx") == "div_23"
    assert _assign_chunk("25 50 00 BAS Sequences.docx") == "controls_commissioning"
    # Unknown CSI prefix → general bucket.
    assert _assign_chunk("33 11 00 Site Utilities.docx") == "general"
    assert _assign_chunk("README.docx") == "general"


def test_group_specs_merges_singletons_into_general():
    specs = [
        _spec("23 05 00 HVAC Common.docx"),
        _spec("23 31 13 Ductwork.docx"),
        _spec("22 11 16 Plumbing.docx"),  # singleton in div_22
    ]
    chunks = _group_specs_by_chunk(specs)
    chunk_ids = [cid for cid, _ in chunks]
    # div_23 has two specs and stays its own chunk; the lone div_22 spec
    # gets merged into "general" so it never gets silently dropped.
    assert "div_23" in chunk_ids
    assert "general" in chunk_ids
    assert "div_22" not in chunk_ids


def test_run_chunked_cross_check_falls_back_to_single_pass(monkeypatch):
    specs = [_spec("23 05 00 a.docx"), _spec("23 31 13 b.docx")]
    captured: dict = {}

    def fake_run_cross_check(s, f, **kwargs):
        captured["specs"] = [x.filename for x in s]
        captured["calls"] = captured.get("calls", 0) + 1
        return ReviewResult(findings=[], cross_check_status="completed")

    monkeypatch.setattr("src.cross_checker.run_cross_check", fake_run_cross_check)
    # Stub the local token estimator so the test does not hit the network
    # to download the tiktoken vocab.
    monkeypatch.setattr("src.cross_checker.count_tokens", lambda text: len(text))

    out = run_chunked_cross_check(specs, [], cycle=DEFAULT_CYCLE)
    assert out.cross_check_status == "completed"
    # Small project: one call to run_cross_check with both specs.
    assert captured["calls"] == 1
    assert captured["specs"] == ["23 05 00 a.docx", "23 31 13 b.docx"]


def test_run_chunked_cross_check_chunks_when_oversized(monkeypatch):
    specs = [
        _spec("21 13 13 Sprinklers.docx"),
        _spec("21 22 00 Clean-Agent.docx"),
        _spec("22 11 16 Plumbing A.docx"),
        _spec("22 14 13 Plumbing B.docx"),
        _spec("23 05 00 HVAC A.docx"),
        _spec("23 31 13 Ductwork.docx"),
    ]
    # Force the wrapper down the chunked path by lowering the budget.
    monkeypatch.setattr("src.cross_checker.CROSS_CHECK_RECOMMENDED_MAX", 10)

    chunk_calls: list[list[str]] = []

    def fake_run_cross_check(s, _f, **kwargs):
        names = [x.filename for x in s]
        chunk_calls.append(names)
        # Return one finding per chunk so synthesis has something to merge.
        return ReviewResult(
            findings=[Finding(
                severity="HIGH", fileName=names[0], section="3.1",
                issue=f"chunk={names[0]}", actionType="EDIT",
                existingText=None, replacementText="x", codeReference=None,
            )],
            cross_check_status="completed",
        )

    monkeypatch.setattr("src.cross_checker.run_cross_check", fake_run_cross_check)
    monkeypatch.setattr("src.cross_checker.count_tokens", lambda text: len(text))

    out = run_chunked_cross_check(specs, [], cycle=DEFAULT_CYCLE)
    assert out.cross_check_status == "completed"
    # Three distinct CSI-division chunks, one call each.
    assert len(chunk_calls) == 3
    assert {chunk_calls[0][0].split()[0]} <= {"21", "22", "23"}
    # Synthesis: one finding per chunk, each section prefixed with the
    # chunk label so the report can tell them apart.
    assert len(out.findings) == 3
    section_labels = " ".join(f.section for f in out.findings)
    assert "Division 21" in section_labels
    assert "Division 22" in section_labels
    assert "Division 23" in section_labels


def test_run_chunked_cross_check_skips_when_chunking_impossible(monkeypatch):
    # Two specs, both in the same division — the chunker cannot split them.
    specs = [
        _spec("23 05 00 a.docx"),
        _spec("23 31 13 b.docx"),
    ]
    monkeypatch.setattr("src.cross_checker.CROSS_CHECK_RECOMMENDED_MAX", 10)

    def boom(*args, **kwargs):
        raise AssertionError("run_cross_check should not be called when chunking is impossible")

    monkeypatch.setattr("src.cross_checker.run_cross_check", boom)
    monkeypatch.setattr("src.cross_checker.count_tokens", lambda text: len(text))

    out = run_chunked_cross_check(specs, [], cycle=DEFAULT_CYCLE)
    assert out.cross_check_status == "skipped"
    assert "exceeds cross-check limit" in (out.thinking or "")


# ---------------------------------------------------------------------------
# Resume-state round-trips include the review mode
# ---------------------------------------------------------------------------


def test_submission_review_mode_round_trips_through_resume_state():
    from src.batch import BatchJob
    from src.pipeline import BatchSubmission
    from src.resume_state import deserialize_submission, serialize_submission

    job = BatchJob(
        batch_id="msgbatch_phase8_test",
        job_type="review",
        request_map={"review__a__0": {"filename": "a.docx", "index": 0, "type": "review"}},
        created_at=1.0,
    )
    submission = BatchSubmission(
        job=job,
        files_reviewed=["a.docx"],
        review_request_ids=["review__a__0"],
        review_mode=ReviewMode.SAFE_EDIT.value,
    )
    payload = serialize_submission(submission)
    assert payload["review_mode"] == ReviewMode.SAFE_EDIT.value

    restored = deserialize_submission(payload)
    assert restored.review_mode == ReviewMode.SAFE_EDIT.value


def test_legacy_submission_payload_defaults_to_comprehensive():
    # Older saved batches predate the review_mode field. Loading them must
    # not crash and should fall back to the default mode.
    from src.resume_state import deserialize_submission

    payload = {
        "job": {
            "batch_id": "msgbatch_legacy",
            "job_type": "review",
            "request_map": {"review__a__0": {"filename": "a.docx"}},
            "created_at": 1.0,
            "status": "submitted",
        },
        "files_reviewed": ["a.docx"],
        "review_request_ids": ["review__a__0"],
        "model": "claude-opus-4-6",
        "project_context": "",
        "code_cycle": "2025",
        "cross_check_enabled": False,
        # No review_mode key.
    }
    restored = deserialize_submission(payload)
    assert restored.review_mode == DEFAULT_REVIEW_MODE.value
