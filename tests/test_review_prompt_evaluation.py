"""Offline controls for the three independent prompt-audit review variants."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from evals import model_effort as me
from src.core import api_config
from src.core.code_cycles import DEFAULT_CYCLE
from src.modules.registry import AVAILABLE_MODULES, get_module
from src.review import prompts
from src.review.review_request_builder import ReviewRequestSpec, build_review_request
from src.review.structured_schemas import REVIEW_OUTPUT_MODES


AUDIT_EXPERIMENTS = (me.EXPERIMENT_REVIEW_MEDIUM, me.EXPERIMENT_REVIEW_PROCEDURE, me.EXPERIMENT_REVIEW_SCOPE)
CURRENT = prompts.REVIEW_PROCEDURE_TEXT[prompts.REVIEW_PROCEDURE_CURRENT]
OPEN_ENDED = prompts.REVIEW_PROCEDURE_TEXT[prompts.REVIEW_PROCEDURE_OPEN_ENDED]


@pytest.mark.parametrize("module_id", sorted(AVAILABLE_MODULES))
@pytest.mark.parametrize("output_mode", REVIEW_OUTPUT_MODES)
def test_open_procedure_changes_only_its_block_in_every_module(monkeypatch, module_id, output_mode):
    cycle = get_module(module_id).cycle
    monkeypatch.delenv(prompts.ENV_REVIEW_PROCEDURE, raising=False)
    baseline = prompts.get_system_prompt(cycle, output_mode=output_mode)
    monkeypatch.setenv(prompts.ENV_REVIEW_PROCEDURE, "open_ended")
    candidate = prompts.get_system_prompt(cycle, output_mode=output_mode)
    assert baseline.count(CURRENT) == 1
    assert candidate == baseline.replace(CURRENT, OPEN_ENDED)
    assert "quote the exact spec text" in candidate
    assert "Do not emit findings for standard boilerplate." in candidate


@pytest.mark.parametrize("module_id", sorted(AVAILABLE_MODULES))
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("repair", [False, True])
def test_primary_and_repair_requests_keep_every_field_except_system(monkeypatch, module_id, batch, repair):
    spec = ReviewRequestSpec(
        spec_content="1.01 SUMMARY\nA. Provide the specified piping system.", filename="23 05 00.docx",
        model=api_config.REVIEW_MODEL_DEFAULT, cycle=get_module(module_id).cycle,
        project_context="Supplied owner basis: keep equipment identifiers consistent.",
        include_service_tier=batch, force_allow_extended_output=False,
        retry_instruction="Return a parseable review payload." if repair else None,
    )
    monkeypatch.delenv(prompts.ENV_REVIEW_PROCEDURE, raising=False)
    baseline = build_review_request(spec)
    monkeypatch.setenv(prompts.ENV_REVIEW_PROCEDURE, "open_ended")
    candidate = build_review_request(spec)
    assert candidate.system_prompt == baseline.system_prompt.replace(CURRENT, OPEN_ENDED)
    assert candidate.user_message == baseline.user_message
    assert {k: v for k, v in candidate.params.items() if k != "system"} == {
        k: v for k, v in baseline.params.items() if k != "system"}
    assert candidate.params["output_config"]["effort"] == "high"


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "current", "unrecognized"])
def test_disabled_or_invalid_procedure_keeps_the_shipped_prompt(monkeypatch, value):
    monkeypatch.delenv(prompts.ENV_REVIEW_PROCEDURE, raising=False)
    baseline = prompts.get_system_prompt(DEFAULT_CYCLE)
    monkeypatch.setenv(prompts.ENV_REVIEW_PROCEDURE, value)
    assert prompts.get_system_prompt(DEFAULT_CYCLE) == baseline


def test_invalid_procedure_warns_once_and_fails_closed(monkeypatch, caplog):
    monkeypatch.setattr(prompts, "_WARNED_PROCEDURE_VALUES", set())
    monkeypatch.setenv(prompts.ENV_REVIEW_PROCEDURE, "always-report")
    with caplog.at_level(logging.WARNING, logger=prompts.__name__):
        first = prompts.get_system_prompt(DEFAULT_CYCLE)
        assert prompts.get_system_prompt(DEFAULT_CYCLE) == first
    assert CURRENT in first
    assert sum(prompts.ENV_REVIEW_PROCEDURE in r.getMessage() for r in caplog.records) == 1


def test_scope_and_procedure_controls_are_independent(monkeypatch):
    monkeypatch.delenv(prompts.ENV_REVIEW_PROCEDURE, raising=False)
    monkeypatch.delenv(prompts.ENV_REVIEW_SCOPE_WORDING, raising=False)
    baseline = prompts.get_system_prompt(DEFAULT_CYCLE)
    monkeypatch.setenv(prompts.ENV_REVIEW_PROCEDURE, "open_ended")
    procedure = prompts.get_system_prompt(DEFAULT_CYCLE)
    assert procedure == baseline.replace(CURRENT, OPEN_ENDED)
    monkeypatch.delenv(prompts.ENV_REVIEW_PROCEDURE)
    monkeypatch.setenv(prompts.ENV_REVIEW_SCOPE_WORDING, "coverage_first")
    scope = prompts.get_system_prompt(DEFAULT_CYCLE)
    assert CURRENT in scope and OPEN_ENDED not in scope
    assert scope == baseline.replace(prompts.REVIEW_SCOPE_EMISSION_SENTENCES["current"],
                                     prompts.REVIEW_SCOPE_EMISSION_SENTENCES["coverage_first"])


def fake_probes(monkeypatch):
    """Preflight-only stub; real fresh-process probes are covered by EX-03 tests."""
    calls = []

    def probe(arm, *, state_root):
        calls.append((arm.arm_id, state_root))
        baseline = {"review.effort": "high", "review.system_prompt_sha256": "shipped"}
        if arm.arm_id == "review_effort_medium":
            baseline["review.effort"] = "medium"
        elif arm.arm_id != me.BASELINE_ARM:
            baseline["review.system_prompt_sha256"] = arm.arm_id
        return baseline

    monkeypatch.setattr(me, "probe_arm_subprocess", probe)
    return calls


@pytest.mark.parametrize("experiment", AUDIT_EXPERIMENTS)
def test_audit_comparisons_preflight_then_alternate_isolated_arms(tmp_path, monkeypatch, experiment):
    probes = fake_probes(monkeypatch)
    calls = []

    def runner(command, *, env, **kwargs):
        calls.append((command, env))
        return SimpleNamespace(returncode=0)

    me.run_experiment(experiment, state_root=tmp_path / "state", out_dir=tmp_path / "out",
                      max_spend_usd=8, repetitions=2, live=True, runner=runner)
    exp = me.EXPERIMENTS[experiment]
    assert [arm for arm, _path in probes] == [exp.baseline, exp.candidate]
    assert [cmd[cmd.index("--arm") + 1] for cmd, _env in calls] == [
        exp.baseline, exp.candidate, exp.candidate, exp.baseline]
    assert len({env["SPEC_CRITIC_CACHE_PATH"] for _cmd, env in calls}) == 4
    for cmd, env in calls:
        arm = me.ARMS[cmd[cmd.index("--arm") + 1]]
        assert me.environment_problems(arm, env) == []
    manifest = json.loads((tmp_path / "out" / f"{experiment}.probes.json").read_text())
    assert manifest["changed_fields"] == sorted(me.ARMS[exp.candidate].expected_request_changes)


@pytest.mark.parametrize("extra_change", [None, "review.fixed_request_sha256", "research_request.sha256"])
def test_preflight_refuses_noop_or_unrelated_changes_before_any_paid_arm(tmp_path, monkeypatch, extra_change):
    def probe(arm, **kwargs):
        result = {"review.effort": "high"}
        if arm.arm_id != me.BASELINE_ARM and extra_change:
            result.update({"review.effort": "medium", extra_change: "unrelated-change"})
        return result

    monkeypatch.setattr(me, "probe_arm_subprocess", probe)
    calls = []
    with pytest.raises(me.RunRefused, match="no paid arm started"):
        me.run_experiment(me.EXPERIMENT_REVIEW_MEDIUM, state_root=tmp_path / "state", out_dir=tmp_path / "out",
            max_spend_usd=8, live=True, runner=lambda *a, **k: calls.append(a))
    assert calls == []
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("cap,repetitions", [(0, 2), (-1, 2), (float("nan"), 2), (float("inf"), 2), (8, 0)])
def test_bad_collection_limits_refuse_before_probes(tmp_path, monkeypatch, cap, repetitions):
    probes = fake_probes(monkeypatch)
    with pytest.raises(me.RunRefused, match="finite positive"):
        me.run_experiment(me.EXPERIMENT_REVIEW_MEDIUM, state_root=tmp_path / "state", out_dir=tmp_path / "out",
                          max_spend_usd=cap, repetitions=repetitions, live=True)
    assert probes == []


def test_failed_arm_stops_subsequent_runs_and_retains_probes(tmp_path, monkeypatch):
    fake_probes(monkeypatch)
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=2)

    with pytest.raises(me.RunRefused, match="no further arms"):
        me.run_experiment(me.EXPERIMENT_REVIEW_MEDIUM, state_root=tmp_path / "state", out_dir=tmp_path / "out",
                          max_spend_usd=8, live=True, runner=runner)
    assert len(calls) == 1
    assert (tmp_path / "out" / f"{me.EXPERIMENT_REVIEW_MEDIUM}.probes.json").exists()


def test_offline_probe_cli_uses_isolated_comparison_without_live_flag(tmp_path, monkeypatch, capsys):
    probes = fake_probes(monkeypatch)
    assert me.main(["probe-experiment", "--experiment", me.EXPERIMENT_REVIEW_MEDIUM,
                    "--state-root", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["changed_fields"] == ["review.effort"]
    assert [arm for arm, _path in probes] == ["baseline", "review_effort_medium"]


@pytest.mark.parametrize("experiment", [me.EXPERIMENT_REVIEW_MEDIUM, me.EXPERIMENT_REVIEW_PROCEDURE])
def test_new_audit_comparisons_cannot_recommend_adoption(experiment):
    assert me.decide(experiment, baseline={}, candidate={}, paired={})["decision"] == me.DECISION_DEFER


def test_measurement_mode_defers_the_existing_scope_experiment():
    decision = me.decide(me.EXPERIMENT_REVIEW_SCOPE, baseline={}, candidate={}, paired={}, measurement_only=True)
    assert decision["decision"] == me.DECISION_DEFER
    assert "total review-plus-verification" in decision["reasons"][0]


def test_measurement_score_cli_reports_deferred_adoption(tmp_path, capsys):
    assert me.main(["score", "--experiment", me.EXPERIMENT_REVIEW_SCOPE,
                    "--out", str(tmp_path), "--measurement-only"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["decision"]["decision"] == me.DECISION_DEFER
    assert result["scores"]["baseline"]["records"] == 0
