"""Chunk 12 — regression tests for the golden-set eval harness.

The harness itself is the regression suite for everything else, so the
tests here are intentionally narrow:

1. The fixture taxonomy covers every category the plan calls out.
2. Running the harness with the checked-in fixtures yields a fully-green
   pass set under the current repaired behavior (the baseline assumption).
3. Each of the ten metrics is computed and reported in the aggregate dict.
4. The runner CLI returns exit-code 0 with the in-repo baseline and 2 on
   simulated drift.

These tests run offline; no Anthropic API key is required.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from evals import BASELINE_PATH
from evals.fixtures import all_fixtures
from evals.harness import HarnessResult, run_harness
from evals.runner import (
    compare_to_baseline,
    load_baseline,
    render_summary,
    write_baseline,
)


pytestmark = pytest.mark.eval_harness


# ---------------------------------------------------------------------------
# Fixture taxonomy
# ---------------------------------------------------------------------------


_EXPECTED_CATEGORIES = frozenset({
    "clean_spec",
    "stale_code_cycle",
    "placeholder",
    "internal_contradiction",
    "coordination",
    "valid_edit",
    "invalid_edit",
    "unsafe_docx",
    "verification_with_source",
    "verification_sourceless_confirmed",
})


def test_fixture_taxonomy_covers_every_required_category():
    """The 10 plan-mandated categories must all appear in the taxonomy."""
    fixtures = all_fixtures()
    present = {fx.category for fx in fixtures}
    missing = _EXPECTED_CATEGORIES - present
    assert not missing, f"Missing categories: {sorted(missing)}"


def test_fixture_ids_are_unique():
    fixtures = all_fixtures()
    ids = [fx.fixture_id for fx in fixtures]
    assert len(ids) == len(set(ids)), f"Duplicate fixture ids: {ids}"


# ---------------------------------------------------------------------------
# End-to-end harness behavior
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def harness_result(tmp_path_factory) -> HarnessResult:
    tmp_dir = tmp_path_factory.mktemp("eval_harness")
    return run_harness(tmp_dir=tmp_dir)


def test_run_harness_executes_every_fixture(harness_result: HarnessResult):
    expected_count = len(all_fixtures())
    assert harness_result.metrics["fixture_count"] == expected_count
    assert harness_result.metrics["fixture_pass_count"] == expected_count
    assert harness_result.metrics["fixture_fail_count"] == 0


_METRIC_KEYS = (
    "review_recall",
    "false_positive_count",
    "duplicate_finding",
    "parse_failure",
    "edit_proposal_validity",
    "locator_success",
    "unsafe_edit_refusal",
    "citation_acceptance",
    "sourceless_confirmed",
    "cost_estimate",
)


def test_harness_reports_every_required_metric(harness_result: HarnessResult):
    for key in _METRIC_KEYS:
        assert key in harness_result.metrics, f"Missing metric: {key}"


def test_invalid_edit_demotes_at_parse_time(harness_result: HarnessResult):
    """Chunk 7 contract: invalid EDIT shapes lose their proposal."""
    invalid = next(
        fr for fr in harness_result.fixtures
        if fr.fixture_id == "invalid_edit_missing_existing"
    )
    assert invalid.demoted_findings == 1
    assert invalid.edit_proposal_valid_count == 0
    assert invalid.review_findings_parsed == 1  # finding itself survives


def test_unsafe_docx_paragraph_refuses_auto_edit(harness_result: HarnessResult):
    """Chunk 9 contract: hyperlink paragraph triggers the unsafe-markup detector."""
    fr = next(
        x for x in harness_result.fixtures
        if x.fixture_id == "unsafe_docx_hyperlink"
    )
    assert fr.unsafe_markup_attempted == 1
    assert fr.unsafe_markup_refused == 1


def test_sourceless_confirmed_is_downgraded(harness_result: HarnessResult):
    """Chunk 5 contract: CONFIRMED without an accepted citation downgrades."""
    fr = next(
        x for x in harness_result.fixtures
        if x.fixture_id == "verification_sourceless_confirmed"
    )
    assert fr.verification_initial_verdict in {"CONFIRMED", "CORRECTED"}
    assert fr.verification_final_verdict == "UNVERIFIED"
    assert fr.accepted_citation_count == 0
    assert fr.downgrade_observed is True


def test_accepted_source_keeps_confirmed(harness_result: HarnessResult):
    fr = next(
        x for x in harness_result.fixtures
        if x.fixture_id == "verification_accepted_source"
    )
    assert fr.verification_final_verdict == "CONFIRMED"
    assert fr.accepted_citation_count == 1


def test_clean_spec_produces_no_false_positives(harness_result: HarnessResult):
    fr = next(x for x in harness_result.fixtures if x.fixture_id == "clean_spec")
    assert fr.review_findings_parsed == 0


def test_locator_succeeds_on_valid_edit(harness_result: HarnessResult):
    fr = next(x for x in harness_result.fixtures if x.fixture_id == "valid_edit")
    assert fr.locator_attempted >= 1
    assert fr.locator_succeeded == fr.locator_attempted


def test_cost_estimate_is_available(harness_result: HarnessResult):
    cost = harness_result.metrics["cost_estimate"]
    assert cost["available"] is True
    assert cost["total_usd"] > 0
    assert cost["currency"] == "USD"


# ---------------------------------------------------------------------------
# Baseline comparison + CLI
# ---------------------------------------------------------------------------


def test_checked_in_baseline_matches_current_run(harness_result: HarnessResult):
    baseline = load_baseline()
    assert baseline is not None, (
        f"Baseline missing at {BASELINE_PATH}; run "
        "`python -m evals.runner --write-baseline`."
    )
    drift = compare_to_baseline(harness_result, baseline)
    assert drift == [], f"Baseline drift detected: {drift}"


def test_baseline_drift_is_reported_when_metrics_change(tmp_path, harness_result):
    """A perturbed baseline should produce drift messages."""
    fake_baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    # Flip recall to 0 so the comparator must flag drift.
    fake_baseline["metrics"]["review_recall"]["rate"] = 0.0
    drift = compare_to_baseline(harness_result, fake_baseline)
    assert any("review_recall" in line for line in drift), drift


def test_runner_cli_exit_code_zero_with_baseline():
    """`python -m evals.runner` matches the checked-in baseline."""
    cmd = [sys.executable, "-m", "evals.runner"]
    result = subprocess.run(
        cmd,
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"runner exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "fail=0" in result.stdout


def test_runner_json_mode_emits_machine_readable_output():
    cmd = [sys.executable, "-m", "evals.runner", "--json"]
    result = subprocess.run(
        cmd,
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "metrics" in payload
    assert "fixtures" in payload
    assert payload["metrics"]["fixture_fail_count"] == 0


def test_render_summary_lists_failed_fixture_issues(harness_result: HarnessResult):
    """Failed fixtures should surface their issue list in the rendered summary."""
    # Build a synthetic FixtureResult with an issue and confirm it renders.
    from evals.harness import FixtureResult
    cloned = HarnessResult(fixtures=list(harness_result.fixtures), metrics=dict(harness_result.metrics))
    cloned.fixtures = list(cloned.fixtures) + [
        FixtureResult(
            fixture_id="synthetic_fail",
            category="synthetic",
            description="Synthetic failing fixture",
            issues=["seeded mismatch: expected 1 got 0"],
        )
    ]
    cloned.metrics = dict(cloned.metrics)
    cloned.metrics["fixture_count"] = cloned.metrics.get("fixture_count", 0) + 1
    cloned.metrics["fixture_fail_count"] = cloned.metrics.get("fixture_fail_count", 0) + 1
    output = render_summary(cloned)
    assert "[FAIL] synthetic_fail" in output
    assert "seeded mismatch" in output


def test_write_baseline_round_trips(tmp_path, harness_result: HarnessResult):
    """The runner can persist + reload a baseline without losing data."""
    out = tmp_path / "baseline.json"
    write_baseline(harness_result, out)
    reloaded = json.loads(out.read_text(encoding="utf-8"))
    assert (
        reloaded["metrics"]["fixture_pass_count"]
        == harness_result.metrics["fixture_pass_count"]
    )
