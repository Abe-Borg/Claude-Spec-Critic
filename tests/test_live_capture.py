"""Hermetic tests for the live-capture harness.

These exercise the pure, non-network parts: the default no-op behavior,
the sentinel-key guard, the review scorer, and — most importantly — that
``build_fixture_dict`` emits JSON the calibration loader accepts. The
network path (``capture`` / ``_run_review``) is intentionally not invoked;
it is gated behind ``--live`` + a real key, which the test sentinel is not.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from evals import live_capture
from evals.calibration.loader import load_fixture
from evals.labeled_specs import (
    LABELED_SPECS,
    ExpectedDefect,
    LabeledSpec,
    defect_matched,
    score_spec_review,
)


def _finding(**kw):
    """A duck-typed stand-in for a parsed Finding."""
    base = dict(
        severity="MEDIUM",
        fileName="f.docx",
        section="1.01",
        issue="",
        actionType="EDIT",
        existingText=None,
        replacementText=None,
        codeReference=None,
        confidence=0.7,
        anchorText=None,
        insertPosition=None,
        evidenceElementId=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _result(**kw):
    """A duck-typed stand-in for a VerificationResult."""
    base = dict(
        verdict="UNVERIFIED",
        explanation="",
        sources=[],
        correction=None,
        model_used="claude-test",
        verification_mode="standard_reasoning",
        verification_profile="code_standard",
        web_search_requests=1,
        successful_source_count=0,
        search_error_count=0,
        searched_sources=[],
        grounded=False,
        cache_status="miss",
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# CLI / guard behavior
# ---------------------------------------------------------------------------


def test_default_invocation_is_hermetic_noop(capsys):
    # No --live: returns 0 and never touches the network.
    assert live_capture.main([]) == 0


def test_live_without_real_key_refuses(monkeypatch, capsys):
    # Force the sentinel explicitly instead of relying on conftest's
    # setdefault: with a real ANTHROPIC_API_KEY exported in the developer's
    # shell, this test previously sailed past the refusal guard and made a
    # REAL paid review call from the hermetic suite. --live must refuse
    # with exit 2 whenever the key is the sentinel.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real-do-not-use")
    assert live_capture.main(["--live"]) == 2


def test_real_key_present_rejects_sentinel(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real-do-not-use")
    assert live_capture.real_key_present() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-looking-value")
    assert live_capture.real_key_present() is True


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_defect_matched_requires_all_tokens():
    defect = ExpectedDefect(
        label="x", expected_severity="MEDIUM", must_match=("ashrae 15", "2019")
    )
    findings = [_finding(issue="Spec cites ASHRAE 15-2019 for the chiller room")]
    assert defect_matched(defect, findings) is findings[0]
    # Missing one token → no match.
    findings2 = [_finding(issue="Spec cites ASHRAE 15 current edition")]
    assert defect_matched(defect, findings2) is None


def test_score_clean_spec_counts_false_positives():
    spec = LabeledSpec(spec_id="c", filename="c.docx", spec_text="", is_clean=True)
    score = score_spec_review(spec, [_finding(), _finding()])
    assert score.false_positive_count == 2
    assert score.matched_defect_count == 0


def test_score_defect_recall_and_severity():
    spec = LabeledSpec(
        spec_id="s",
        filename="s.docx",
        spec_text="",
        expected_defects=(
            ExpectedDefect(
                label="stale", expected_severity="MEDIUM", must_match=("2019",)
            ),
        ),
    )
    findings = [_finding(severity="MEDIUM", existingText="Comply with 2019 CBC")]
    score = score_spec_review(spec, findings)
    assert score.matched_defect_count == 1
    assert score.severity_match_count == 1
    # A different severity still counts as found, just not a severity match.
    findings_hi = [_finding(severity="HIGH", existingText="Comply with 2019 CBC")]
    score_hi = score_spec_review(spec, findings_hi)
    assert score_hi.matched_defect_count == 1
    assert score_hi.severity_match_count == 0


# ---------------------------------------------------------------------------
# Serialization round-trips through the calibration loader
# ---------------------------------------------------------------------------


def test_build_fixture_dict_is_loader_valid(tmp_path):
    spec = LABELED_SPECS[1]  # stale_cbc, has one labeled defect
    defect = spec.expected_defects[0]
    finding = _finding(
        severity="MEDIUM",
        fileName=spec.filename,
        issue="Cites 2019 CBC for a 2025 project",
        existingText="2019 CBC",
        replacementText="2025 CBC",
        codeReference="CBC 2025",
    )
    result = _result(verdict="CORRECTED", grounded=True, sources=["https://dgs.ca.gov"])
    fixture = live_capture.build_fixture_dict(
        spec, finding, result, defect, cycle_label="2025", index=0
    )
    path = tmp_path / f"{fixture['fixture_id']}.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")

    loaded = load_fixture(path)  # raises on any missing/invalid key
    assert loaded.fixture_id == "live_stale_cbc_0"
    # Ground truth seeds from the defect LABEL (not the captured verdict) —
    # assert against the label itself so a relabel can't break this test.
    assert loaded.ground_truth.correct_verdict == defect.expected_verdict
    assert loaded.captured_verifier_response.searched_urls == []
    assert loaded.finding.existingText == "2019 CBC"


def test_build_fixture_dict_unlabeled_seeds_from_capture(tmp_path):
    spec = LABELED_SPECS[0]  # clean spec, no defects
    finding = _finding(issue="some extra finding")
    result = _result(verdict="UNVERIFIED")
    fixture = live_capture.build_fixture_dict(
        spec, finding, result, None, cycle_label="2025", index=3
    )
    path = tmp_path / f"{fixture['fixture_id']}.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    loaded = load_fixture(path)
    assert loaded.ground_truth.correct_verdict == "UNVERIFIED"
