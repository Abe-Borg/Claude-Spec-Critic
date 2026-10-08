"""Plan EX-03 (chunk S22): model, effort, and confidence — offline.

Nothing here sends a request. The tests pin:

1. the two default-off switches the experiment adds — ``SPEC_CRITIC_REVIEW_EFFORT``
   and ``SPEC_CRITIC_REVIEW_SCOPE_WORDING`` — byte-identical when off, one
   change when on, failing closed on anything else;
2. the adjudicated dataset: valid, every required dimension in both splits,
   held-out cases new (never tuned on), external sources only, digests that
   move when content moves, and references to the oracle ledger that re-check
   the evidence they were adjudicated on;
3. the arms: one setting each, an environment that drops everything the
   operator's shell carries, state that starts empty, and — built in a fresh
   process per arm — requests that differ from the baseline in exactly the
   fields the arm names;
4. cache isolation: why it matters (the cache key ignores the model) and that
   the runner gives every arm and view a fresh cache with no hits;
5. the runner against scripted clients (records, the spending cap, refusals);
6. the scorer and the pre-registered decision rules.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

import src.verification.verifier as V
from evals import model_effort as me
from evals import model_effort_dataset as ds
from src.core import api_config
from src.core.code_cycles import DEFAULT_CYCLE
from src.modules.registry import AVAILABLE_MODULES, get_module
from src.review import prompts
from src.review import realtime_review as rt
from src.review.review_request_builder import ReviewRequestSpec, build_review_request
from src.review.reviewer import Finding
from src.verification.verification_cache import VerificationCache
from src.verification.verification_routing import select_routing
from tests.fixtures.fake_anthropic import review_tool_use_response
from tests.fixtures.verification_drivers import (
    ScriptedStreamClient,
    message,
    search_blocks,
    verdict_call,
    verdict_payload,
)


def _review_params(*, model: str = api_config.REVIEW_MODEL_DEFAULT, cycle=DEFAULT_CYCLE) -> dict:
    return build_review_request(
        ReviewRequestSpec(
            spec_content="PART 1 GENERAL\n1.01 SUMMARY\nA. Provide the specified piping system.",
            filename="230500.docx",
            model=model,
            cycle=cycle,
            include_service_tier=False,
        )
    ).params


def _clear_experiment_env(monkeypatch) -> None:
    for name in list(os.environ):
        if name.startswith("SPEC_CRITIC_"):
            monkeypatch.delenv(name, raising=False)


# ===========================================================================
# 1. The review effort switch
# ===========================================================================


class TestReviewEffortSwitch:
    def test_unset_is_the_phase_default(self, monkeypatch) -> None:
        monkeypatch.delenv(api_config.ENV_REVIEW_EFFORT, raising=False)
        assert api_config.review_effort_override() is None
        # The phase declares high, and the Sonnet 5.5 review model takes it.
        assert _review_params()["output_config"] == {"effort": "high"}

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "OFF", "  off  "])
    def test_off_values_are_byte_identical(self, monkeypatch, value) -> None:
        monkeypatch.delenv(api_config.ENV_REVIEW_EFFORT, raising=False)
        baseline = json.dumps(_review_params(), sort_keys=True, default=str)
        monkeypatch.setenv(api_config.ENV_REVIEW_EFFORT, value)
        assert json.dumps(_review_params(), sort_keys=True, default=str) == baseline

    @pytest.mark.parametrize("value", ["low", "medium", "high", "xhigh", "XHIGH"])
    def test_a_level_sets_the_review_effort_and_nothing_else(self, monkeypatch, value) -> None:
        monkeypatch.delenv(api_config.ENV_REVIEW_EFFORT, raising=False)
        baseline = _review_params()
        monkeypatch.setenv(api_config.ENV_REVIEW_EFFORT, value)
        params = _review_params()
        assert params["output_config"] == {"effort": value.lower()}
        changed = sorted(k for k in set(params) | set(baseline) if params.get(k) != baseline.get(k))
        assert changed == ([] if value.lower() == "high" else ["output_config"])

    def test_unknown_value_keeps_the_default_and_warns_once(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(api_config, "_WARNED_REVIEW_EFFORT_VALUES", set())
        monkeypatch.setenv(api_config.ENV_REVIEW_EFFORT, "max")
        with caplog.at_level(logging.WARNING, logger=api_config.__name__):
            assert _review_params()["output_config"] == {"effort": "high"}
            _review_params()
        assert sum(api_config.ENV_REVIEW_EFFORT in r.getMessage() for r in caplog.records) == 1

    def test_xhigh_is_clamped_on_a_model_without_it(self, monkeypatch) -> None:
        monkeypatch.setenv(api_config.ENV_REVIEW_EFFORT, "xhigh")
        assert _review_params(model=api_config.MODEL_SONNET_46)["output_config"] == {"effort": "high"}

    def test_a_model_without_effort_sends_none(self, monkeypatch) -> None:
        monkeypatch.setenv(api_config.ENV_REVIEW_EFFORT, "xhigh")
        assert "output_config" not in _review_params(model=api_config.MODEL_HAIKU_45)

    def test_other_phases_keep_their_effort(self, monkeypatch) -> None:
        monkeypatch.setenv(api_config.ENV_REVIEW_EFFORT, "xhigh")
        for phase, model in (
            (api_config.PHASE_CROSS_CHECK, api_config.CROSS_CHECK_MODEL_DEFAULT),
            (api_config.PHASE_COMPLIANCE, api_config.COMPLIANCE_MODEL_DEFAULT),
            (api_config.PHASE_VERIFICATION, api_config.VERIFICATION_MODEL_DEFAULT),
        ):
            assert api_config.effort_config_for(model=model, phase=phase)["effort"] in ("high", "medium")

    def test_no_phase_default_moved(self) -> None:
        # The switch is an override, not a default; the ceiling pin still holds.
        assert all(level in ("low", "medium", "high") for level in api_config._PHASE_DEFAULT_EFFORT.values())


# ===========================================================================
# 2. The <review_scope> wording switch
# ===========================================================================

_CURRENT = prompts.REVIEW_SCOPE_EMISSION_SENTENCES[prompts.REVIEW_SCOPE_WORDING_CURRENT]
_COVERAGE_FIRST = prompts.REVIEW_SCOPE_EMISSION_SENTENCES[prompts.REVIEW_SCOPE_WORDING_COVERAGE_FIRST]


class TestReviewScopeWordingSwitch:
    def test_unset_keeps_the_current_sentence(self, monkeypatch) -> None:
        monkeypatch.delenv(prompts.ENV_REVIEW_SCOPE_WORDING, raising=False)
        prompt = prompts.get_system_prompt(DEFAULT_CYCLE)
        assert _CURRENT in prompt and _COVERAGE_FIRST not in prompt

    @pytest.mark.parametrize("module_id", sorted(AVAILABLE_MODULES))
    def test_coverage_first_replaces_exactly_one_sentence(self, monkeypatch, module_id) -> None:
        cycle = get_module(module_id).cycle
        monkeypatch.delenv(prompts.ENV_REVIEW_SCOPE_WORDING, raising=False)
        before = prompts.get_system_prompt(cycle)
        monkeypatch.setenv(prompts.ENV_REVIEW_SCOPE_WORDING, "coverage_first")
        after = prompts.get_system_prompt(cycle)
        assert before.count(_CURRENT) == 1
        assert after == before.replace(_CURRENT, _COVERAGE_FIRST)

    def test_the_candidate_is_a_grounding_rule_not_a_certainty_bar(self) -> None:
        assert "quote the spec text" in _COVERAGE_FIRST
        assert "confidence" in _COVERAGE_FIRST
        assert "Only report" not in _COVERAGE_FIRST

    @pytest.mark.parametrize("value", ["", "0", "off", "current"])
    def test_off_and_unknown_values_keep_the_prompt(self, monkeypatch, value) -> None:
        monkeypatch.delenv(prompts.ENV_REVIEW_SCOPE_WORDING, raising=False)
        before = prompts.get_system_prompt(DEFAULT_CYCLE)
        monkeypatch.setenv(prompts.ENV_REVIEW_SCOPE_WORDING, value)
        assert prompts.get_system_prompt(DEFAULT_CYCLE) == before

    def test_unknown_value_warns_once(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(prompts, "_WARNED_SCOPE_WORDING_VALUES", set())
        monkeypatch.setenv(prompts.ENV_REVIEW_SCOPE_WORDING, "lenient")
        with caplog.at_level(logging.WARNING, logger=prompts.__name__):
            prompts.get_system_prompt(DEFAULT_CYCLE)
            prompts.get_system_prompt(DEFAULT_CYCLE)
        assert sum(prompts.ENV_REVIEW_SCOPE_WORDING in r.getMessage() for r in caplog.records) == 1

    def test_the_request_carries_the_switched_prompt(self, monkeypatch) -> None:
        monkeypatch.setenv(prompts.ENV_REVIEW_SCOPE_WORDING, "coverage_first")
        system = _review_params()["system"]
        text = system if isinstance(system, str) else "".join(block["text"] for block in system)
        assert _COVERAGE_FIRST in text


# ===========================================================================
# 3. The dataset
# ===========================================================================


@pytest.fixture(scope="module")
def cases():
    return ds.load_dataset()


class TestDataset:
    def test_is_valid(self, cases) -> None:
        assert ds.validate_dataset(cases) == []

    def test_every_dimension_is_in_both_splits(self, cases) -> None:
        summary = ds.summarize(cases)
        for dim, counts in summary["dimensions"].items():
            assert counts[ds.SPLIT_TUNING] >= 1, dim
            assert counts[ds.SPLIT_HELD_OUT] >= 1, dim

    def test_held_out_cases_are_new(self, cases) -> None:
        for case in cases:
            if case.split == ds.SPLIT_HELD_OUT:
                assert not case.prior_exposure, case.case_id
                assert case.provenance.startswith("new"), case.case_id
            if case.provenance.split(":", 1)[0] in ("oracle_ledger", "labeled_spec", "dc_scenario"):
                assert case.split == ds.SPLIT_TUNING, case.case_id

    def test_both_failure_directions_are_measurable(self, cases) -> None:
        held = ds.verification_cases(cases, split=ds.SPLIT_HELD_OUT)
        verdicts = [c.expected_verdict for c in held]
        # False CONFIRMED needs non-CONFIRMED truths; false DISPUTED needs
        # non-DISPUTED truths; and uncertainty needs cases that should stay open.
        assert verdicts.count("CONFIRMED") >= 5
        assert verdicts.count("DISPUTED") >= 5
        assert verdicts.count("UNVERIFIED") >= 2
        assert sum(c.severe and c.expected_verdict == "CONFIRMED" for c in held) >= 3

    def test_held_out_reaches_the_escalation_tier_without_the_gate(self, cases) -> None:
        # CRITICAL + jurisdictional routes the initial pass to DEEP_REASONING,
        # so the path view measures the escalation model on these directly.
        deep = [
            c for c in ds.verification_cases(cases, split=ds.SPLIT_HELD_OUT)
            if select_routing(Finding(**dict(c.finding)), local_skip=False,
                              cycle=get_module(c.module_id).cycle).mode.value == "deep_reasoning"
        ]
        assert len(deep) >= 2

    def test_severe_review_defects_meet_the_minimum(self, cases) -> None:
        held = ds.review_cases(cases, split=ds.SPLIT_HELD_OUT)
        assert sum(len(c.severe_defects) for c in held) >= ds.MIN_HELD_OUT_SEVERE_REVIEW_DEFECTS
        assert any(c.is_clean for c in held)
        assert sum(len(c.traps) for c in held) >= 10

    def test_digests_are_stable_and_move_with_content(self, cases) -> None:
        again = ds.load_dataset()
        assert ds.dataset_digest(cases) == ds.dataset_digest(again)
        case = ds.NEW_VERIFICATION_CASES[0]
        changed = ds.VerificationCase(**{**case.__dict__, "rationale": case.rationale + " "})
        assert ds.case_digest(changed) != ds.case_digest(case)
        swapped = [changed if c.case_id == case.case_id else c for c in cases]
        assert ds.dataset_digest(swapped) != ds.dataset_digest(cases)
        assert ds.dataset_digest(swapped, split=ds.SPLIT_TUNING) == ds.dataset_digest(cases, split=ds.SPLIT_TUNING)

    def test_a_changed_capture_refuses_to_load(self, tmp_path, monkeypatch) -> None:
        live = Path(ds.__file__).parent / "calibration" / "fixtures_live"
        for src in live.glob("*.json"):
            (tmp_path / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        path = tmp_path / "live_stale_cbc_0.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["captured_verifier_response"]["verdict"] = "DISPUTED"
        path.write_text(json.dumps(raw), encoding="utf-8")
        monkeypatch.setattr(ds, "_LIVE_FIXTURES_DIR", tmp_path)
        with pytest.raises(ValueError, match="re-adjudicate"):
            ds.load_dataset()

    def test_ledger_repository_sources_do_not_carry_over(self, cases) -> None:
        for case in cases:
            for src in case.sources:
                assert "src/" not in src.citation and ".py" not in src.citation, case.case_id


class TestDatasetValidationCatches:
    """Each rule, broken on purpose, is reported (mutation checks on the validator)."""

    def _one(self, cases, case_id, **changes):
        out = []
        for c in cases:
            if c.case_id == case_id:
                c = type(c)(**{**c.__dict__, **changes})
            out.append(c)
        return out

    def test_prior_exposure_in_held_out(self, cases) -> None:
        bad = self._one(cases, "v-nfpa13-oh-coverage-225", prior_exposure="used in the S18 tuning")
        assert any("tuning split" in p for p in ds.validate_dataset(bad))

    def test_a_repository_source(self, cases) -> None:
        bad = self._one(cases, "v-nfpa13-oh-coverage-225",
                        sources=(ds.Source(citation="src/core/code_cycles.py pins NFPA 13-2022"),))
        assert any("cites this repository" in p for p in ds.validate_dataset(bad))

    def test_a_constructed_case_that_expects_a_conclusion(self, cases) -> None:
        bad = self._one(cases, "v-fictional-release-panel-approval", expected_verdict="DISPUTED")
        assert any("constructed" in p for p in ds.validate_dataset(bad))

    def test_an_external_case_without_a_source(self, cases) -> None:
        bad = self._one(cases, "v-nfpa20-150-percent-head", sources=())
        assert any("needs a source" in p for p in ds.validate_dataset(bad))

    def test_two_cases_with_one_cache_key(self, cases) -> None:
        twin = next(c for c in cases if c.case_id == "v-nfpa20-150-percent-head")
        dup = ds.VerificationCase(**{**twin.__dict__, "case_id": "v-twin"})
        assert any("cache key" in p for p in ds.validate_dataset([*cases, dup]))

    def test_a_dimension_missing_from_a_split(self, cases) -> None:
        bad = [c for c in cases if not (c.split == ds.SPLIT_HELD_OUT and "severe_omission" in c.dimensions)]
        assert any("severe_omission" in p and "held_out" in p for p in ds.validate_dataset(bad))

    def test_a_clean_case_with_defects(self, cases) -> None:
        bad = self._one(cases, "r-dc-preaction-data-hall", is_clean=True)
        assert any("clean case carries defects" in p for p in ds.validate_dataset(bad))

    def test_an_acceptable_verdict_equal_to_the_expected(self, cases) -> None:
        bad = self._one(cases, "v-fm-ds-5-32-delete-as-noncode", acceptable_verdicts=("DISPUTED",))
        assert any("distinct verdict" in p for p in ds.validate_dataset(bad))

    def test_too_few_held_out_severe_defects(self, cases) -> None:
        bad = [c for c in cases if not (isinstance(c, ds.ReviewCase) and c.split == ds.SPLIT_HELD_OUT
                                        and c.case_id in ("r-dc-fire-pump", "r-dc-preaction-data-hall"))]
        assert any("severe held-out review defects" in p for p in ds.validate_dataset(bad))


# ===========================================================================
# 4. Arms, environments, and state
# ===========================================================================


class TestArms:
    def test_registry_is_one_change_per_arm(self) -> None:
        assert me.one_change_problems() == []
        for exp in me.EXPERIMENTS.values():
            assert me.ARMS[exp.candidate].setting is not None
            assert exp.baseline == me.BASELINE_ARM

    def test_the_rule_catches_a_second_setting_and_a_duplicate_setting(self) -> None:
        arms = dict(me.ARMS)
        arms["twin"] = me.Arm("twin", (me.ENV_REVIEW_EFFORT, "xhigh"), "x", ("review.effort",))
        problems = me.one_change_problems(arms)
        assert any("same setting" in p for p in problems)
        assert any("exactly one experiment" in p for p in problems)
        arms = {**me.ARMS, "none": me.Arm("none", None, "x")}
        assert any("exactly one variable" in p for p in me.one_change_problems(arms))

    def test_the_escalation_arm_is_not_the_initial_model(self) -> None:
        # A Sonnet 5 escalation would equal the initial verifier, and the gate
        # refuses to escalate to the initial model: two changes, not one.
        name, value = me.ARMS["escalation_opus_4_8"].setting
        assert name == me.ENV_ESCALATION_MODEL
        assert value != api_config.VERIFICATION_MODEL_DEFAULT
        assert value in api_config.OPUS_MODELS  # so it runs at the baseline's (Opus) effort
        assert api_config.model_capabilities(value).supports_web_fetch


class TestArmEnvironment:
    BASE = {
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": "k",
        "SPEC_CRITIC_GOVERNING_BASIS_CONTEXT": "1",
        "SPEC_CRITIC_REVIEW_OUTPUT_CONSTRAINT": "json_schema",
        "SPEC_CRITIC_PROJECT_CONTEXT_CACHE": "1h",
        "SPEC_CRITIC_REVIEW_PROCEDURE": "open_ended",
        "SPEC_CRITIC_VERIFICATION_ESCALATION_MODEL": "claude-sonnet-4-6",
        "SPEC_CRITIC_CACHE_PATH": "/home/me/.spec_critic/verification_cache.json",
    }

    @pytest.mark.parametrize("arm_id", sorted(me.ARMS))
    def test_inherited_settings_are_dropped_and_one_is_added(self, tmp_path, arm_id) -> None:
        arm = me.ARMS[arm_id]
        env = me.arm_environment(arm, state_dir=tmp_path, base_env=self.BASE)
        assert env["PATH"] == "/usr/bin" and env["ANTHROPIC_API_KEY"] == "k"
        assert me.arm_settings(env) == (dict([arm.setting]) if arm.setting else {})
        for name in me.ARM_STATE_ENV:
            assert Path(env[name]).is_relative_to(tmp_path), name
        assert me.environment_problems(arm, env) == []

    def test_problems_name_a_leftover_setting_and_a_missing_path(self, tmp_path) -> None:
        arm = me.ARMS[me.BASELINE_ARM]
        env = me.arm_environment(arm, state_dir=tmp_path, base_env={})
        env["SPEC_CRITIC_REVIEW_EFFORT"] = "xhigh"
        del env["SPEC_CRITIC_PENDING_BATCH_PATH"]
        problems = me.environment_problems(arm, env)
        assert any("experimental settings" in p for p in problems)
        assert any("SPEC_CRITIC_PENDING_BATCH_PATH" in p for p in problems)

    def test_state_dir_must_start_empty(self, tmp_path) -> None:
        arm = me.ARMS[me.BASELINE_ARM]
        state = me.prepare_state_dir(tmp_path, arm)
        (state / "verification_cache.json").write_text("{}", encoding="utf-8")
        with pytest.raises(me.ArmStateError, match="already holds state"):
            me.prepare_state_dir(tmp_path, arm)

    def test_state_dir_never_inside_the_apps_own(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(me, "_operator_state_dirs", lambda: [tmp_path])
        with pytest.raises(me.ArmStateError, match="own state directory"):
            me.prepare_state_dir(tmp_path / "evals", me.ARMS[me.BASELINE_ARM])


@pytest.fixture(scope="module")
def arm_probes(tmp_path_factory):
    """Each arm's request probe, built in a fresh process with its environment.

    The parent carries two unrelated experiment switches; the arms must not.
    """
    root = tmp_path_factory.mktemp("arm-probes")
    base_env = dict(os.environ)
    base_env["SPEC_CRITIC_REVIEW_OUTPUT_CONSTRAINT"] = "json_schema"
    base_env["SPEC_CRITIC_GOVERNING_BASIS_CONTEXT"] = "1"
    base_env["SPEC_CRITIC_REVIEW_PROCEDURE"] = "open_ended"
    base_env["SPEC_CRITIC_REVIEW_EFFORT"] = "xhigh"
    base_env["SPEC_CRITIC_REVIEW_SCOPE_WORDING"] = "coverage_first"
    return {arm_id: me.probe_arm_subprocess(arm, state_root=root, base_env=base_env)
            for arm_id, arm in me.ARMS.items()}


class TestOneChangePerArmAtTheRequest:
    @pytest.mark.parametrize("arm_id", [a for a in me.ARMS if a != me.BASELINE_ARM])
    def test_the_arm_changes_exactly_its_fields(self, arm_probes, arm_id) -> None:
        diff = me.probe_differences(arm_probes[me.BASELINE_ARM], arm_probes[arm_id])
        assert diff == sorted(me.ARMS[arm_id].expected_request_changes)

    def test_the_baseline_is_the_apps_default(self, arm_probes) -> None:
        base = arm_probes[me.BASELINE_ARM]
        assert base["review.effort"] == "high"
        assert base["review.model"] == api_config.MODEL_SONNET_55
        assert base["review.tool_choice"]["type"] == "auto"  # the parent's json_schema did not leak
        assert base["escalation.model"] == api_config.MODEL_OPUS_55
        assert "web_fetch" not in base["escalation.tools"]
        assert base["escalation_gate.fires_on_unresolved_high"] is True

    def test_the_escalation_arm_keeps_effort_and_the_gate(self, arm_probes) -> None:
        arm = arm_probes["escalation_opus_4_8"]
        assert arm["escalation.model"] == "claude-opus-4-8"
        assert arm["escalation.effort"] == "medium"
        assert "web_fetch" in arm["escalation.tools"]
        assert arm["escalation_gate.fires_on_unresolved_high"] is True
        assert arm["verification_initial.model"] == api_config.VERIFICATION_MODEL_DEFAULT


# ===========================================================================
# 5. Cache isolation
# ===========================================================================


def _grounded_result(model: str) -> V.VerificationResult:
    return V.VerificationResult(
        verdict="CONFIRMED",
        explanation="x",
        sources=["https://www.nfpa.org/codes-and-standards/nfpa-13"],
        accepted_sources=["https://www.nfpa.org/codes-and-standards/nfpa-13"],
        grounded=True,
        model_used=model,
        source_quote="130 ft2",
        outcome=V.OUTCOME_VERDICT,
    )


class TestCacheIsolation:
    def test_one_shared_cache_would_replay_across_arms(self) -> None:
        # Why arms need their own caches: the key ignores the model, so the
        # baseline's Opus 5.5 verdict answers the candidate's question too.
        finding = Finding(**dict(ds.NEW_VERIFICATION_CASES[0].finding))
        shared = VerificationCache()
        shared.put(finding, cycle=get_module("datacenter_fire").cycle, result=_grounded_result(api_config.MODEL_OPUS_55))
        hit = shared.get(finding, cycle=get_module("datacenter_fire").cycle)
        assert hit is not None and hit.model_used == api_config.MODEL_OPUS_55
        fresh = VerificationCache()
        assert fresh.get(finding, cycle=get_module("datacenter_fire").cycle) is None


# ===========================================================================
# 6. The runner, against scripted clients
# ===========================================================================


def _enter_arm(monkeypatch, tmp_path, arm_id: str) -> Path:
    _clear_experiment_env(monkeypatch)
    state = me.prepare_state_dir(tmp_path / "state", me.ARMS[arm_id])
    for name, value in me.arm_environment(me.ARMS[arm_id], state_dir=state, base_env={}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-hermetic-tests")
    return state


def _verified(*_a, **_k):
    return message(search_blocks() + [verdict_call(verdict_payload("CONFIRMED"))])


@pytest.fixture
def verification_client(monkeypatch):
    client = ScriptedStreamClient(_verified)
    monkeypatch.setattr(V, "_get_client", lambda **_: client)
    monkeypatch.setattr(V.time, "sleep", lambda _s: None)
    return client


def _held_out(case_ids):
    return [c for c in ds.load_dataset() if c.case_id in case_ids]


class TestRunnerVerification:
    CASES = ("v-nfpa13-oh-coverage-225", "v-nfpa13-metric-conversion-false", "v-dsa-seismic-bracing-omitted")

    def test_records_every_case_in_both_views_with_fresh_caches(self, monkeypatch, tmp_path, verification_client) -> None:
        _enter_arm(monkeypatch, tmp_path, me.BASELINE_ARM)
        out = tmp_path / "out"
        summary = me.run_arm(me.BASELINE_ARM, stage=ds.STAGE_VERIFICATION, out_dir=out,
                             max_spend_usd=100.0, live=True, cases=_held_out(self.CASES))
        records = me.load_records(out, me.BASELINE_ARM, ds.STAGE_VERIFICATION)
        assert {(r["case_id"], r["view"]) for r in records} == {(c, v) for c in self.CASES for v in me.VIEWS}
        assert all(r["status"] == "ok" and r["verdict"] == "CONFIRMED" for r in records)
        assert summary["cache_contaminated"] is False
        assert all(stats["hits"] == 0 and stats["loaded_from_disk"] == 0 for stats in summary["cache_stats"].values())
        assert summary["estimated_spend_usd"] > 0
        tier = [r for r in records if r["view"] == me.VIEW_TIER]
        assert all(r["verification_mode"] == "deep_reasoning" for r in tier)
        # Nothing was written to the arm's disk cache path.
        assert not Path(os.environ["SPEC_CRITIC_CACHE_PATH"]).exists()

    def test_the_spending_cap_stops_between_cases(self, monkeypatch, tmp_path, verification_client) -> None:
        _enter_arm(monkeypatch, tmp_path, me.BASELINE_ARM)
        out = tmp_path / "out"
        summary = me.run_arm(me.BASELINE_ARM, stage=ds.STAGE_VERIFICATION, out_dir=out,
                             max_spend_usd=1e-9, live=True, cases=_held_out(self.CASES))
        records = me.load_records(out, me.BASELINE_ARM, ds.STAGE_VERIFICATION)
        not_run = [r for r in records if r["status"] == "not_run"]
        assert len(not_run) == 2 and "spending cap" in summary["stopped_reason"]
        assert len({r["case_id"] for r in records if r["status"] == "ok"}) == 1

    def test_the_scored_records_read_back(self, monkeypatch, tmp_path, verification_client) -> None:
        _enter_arm(monkeypatch, tmp_path, me.BASELINE_ARM)
        out = tmp_path / "out"
        cases = _held_out(self.CASES)
        me.run_arm(me.BASELINE_ARM, stage=ds.STAGE_VERIFICATION, out_dir=out, max_spend_usd=100.0,
                   live=True, cases=cases)
        score = me.score_verification(me.load_records(out, me.BASELINE_ARM, ds.STAGE_VERIFICATION),
                                      cases, view=me.VIEW_PATH)
        # The scripted verifier CONFIRMs everything: one of the three truths is DISPUTED.
        assert score["accuracy"]["count"] == 2 and score["accuracy"]["n"] == 3
        assert score["false_confirmed"]["count"] == 1 and score["false_disputed"]["count"] == 0


class TestRunnerRefuses:
    def _refused(self, monkeypatch, tmp_path, **kwargs):
        with pytest.raises(me.RunRefused) as exc:
            me.run_arm(me.BASELINE_ARM, stage=ds.STAGE_VERIFICATION, out_dir=tmp_path / "out",
                       cases=_held_out(TestRunnerVerification.CASES), **kwargs)
        return str(exc.value)

    def test_without_live(self, monkeypatch, tmp_path) -> None:
        _enter_arm(monkeypatch, tmp_path, me.BASELINE_ARM)
        assert "--live" in self._refused(monkeypatch, tmp_path, max_spend_usd=1.0)

    def test_without_a_cap(self, monkeypatch, tmp_path) -> None:
        _enter_arm(monkeypatch, tmp_path, me.BASELINE_ARM)
        assert "spending cap" in self._refused(monkeypatch, tmp_path, max_spend_usd=0, live=True)

    def test_with_the_test_sentinel_key(self, monkeypatch, tmp_path) -> None:
        _enter_arm(monkeypatch, tmp_path, me.BASELINE_ARM)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real-do-not-use")
        assert "ANTHROPIC_API_KEY" in self._refused(monkeypatch, tmp_path, max_spend_usd=1.0, live=True)

    def test_in_another_arms_environment(self, monkeypatch, tmp_path) -> None:
        _enter_arm(monkeypatch, tmp_path, "review_effort_xhigh")
        assert "experimental settings" in self._refused(monkeypatch, tmp_path, max_spend_usd=1.0, live=True)

    def test_when_the_process_resolved_another_escalation_model(self, monkeypatch, tmp_path) -> None:
        _enter_arm(monkeypatch, tmp_path, "escalation_opus_4_8")
        # The env says Opus 4.8, but this process imported api_config without it.
        with pytest.raises(me.RunRefused, match="fresh process"):
            me.run_arm("escalation_opus_4_8", stage=ds.STAGE_VERIFICATION, out_dir=tmp_path / "out",
                       max_spend_usd=1.0, live=True, cases=_held_out(TestRunnerVerification.CASES))

    def test_onto_existing_records(self, monkeypatch, tmp_path, verification_client) -> None:
        _enter_arm(monkeypatch, tmp_path, me.BASELINE_ARM)
        me.run_arm(me.BASELINE_ARM, stage=ds.STAGE_VERIFICATION, out_dir=tmp_path / "out",
                   max_spend_usd=100.0, live=True, cases=_held_out(TestRunnerVerification.CASES))
        assert "already has records" in self._refused(monkeypatch, tmp_path, max_spend_usd=1.0, live=True)

    def test_the_cli_refuses_without_writing(self, monkeypatch, tmp_path, capsys) -> None:
        _enter_arm(monkeypatch, tmp_path, me.BASELINE_ARM)
        code = me.main(["run", "--arm", me.BASELINE_ARM, "--stage", "verification",
                        "--out", str(tmp_path / "out"), "--max-spend-usd", "1"])
        assert code == 2 and "refused" in capsys.readouterr().err
        assert not (tmp_path / "out").exists()


class TestRunnerReview:
    def test_records_findings_from_the_production_path(self, monkeypatch, tmp_path) -> None:
        _enter_arm(monkeypatch, tmp_path, me.BASELINE_ARM)
        monkeypatch.setattr(rt, "review_extended_output_count", lambda request_spec: 100)
        payload = {
            "analysis_summary": "",
            "findings": [
                {
                    "severity": "HIGH", "fileName": "x.docx", "section": "3.01",
                    "issue": "Ordinary hazard coverage of 225 sq ft exceeds 130 sq ft.",
                    "actionType": "EDIT",
                    "existingText": "Maximum protection area per sprinkler: 225 sq ft.",
                    "replacementText": "Maximum protection area per sprinkler: 130 sq ft.",
                    "codeReference": "NFPA 13", "confidence": 0.9, "anchorText": None,
                    "insertPosition": None, "evidenceElementId": None,
                },
                {
                    "severity": "HIGH", "fileName": "x.docx", "section": "3.01",
                    "issue": "Provide sprinklers in the electrical room.",
                    "actionType": "EDIT", "existingText": "electrical room",
                    "replacementText": "electrical room with sprinklers",
                    "codeReference": "NFPA 13", "confidence": 0.7, "anchorText": None,
                    "insertPosition": None, "evidenceElementId": None,
                },
            ],
        }
        calls = []

        class _Stream:
            def __init__(self, msg):
                self._msg = msg

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            text_stream = property(lambda self: iter(()))

            def get_final_message(self):
                return self._msg

            def __iter__(self):
                return iter(())

        class _Client:
            class messages:  # noqa: N801 - mimics the SDK attribute
                @staticmethod
                def stream(**kwargs):
                    calls.append(kwargs)
                    return _Stream(review_tool_use_response(payload=payload))

        monkeypatch.setattr(rt, "_get_client", lambda **_: _Client())
        cases = _held_out(("r-dc-preaction-data-hall",))
        out = tmp_path / "out"
        summary = me.run_arm(me.BASELINE_ARM, stage=ds.STAGE_REVIEW, out_dir=out, max_spend_usd=100.0,
                             live=True, cases=cases)
        assert summary["cases"] == 1 and not summary["stopped_reason"]
        (record,) = me.load_records(out, me.BASELINE_ARM, ds.STAGE_REVIEW)
        assert record["parse_status"] == "ok" and len(record["findings"]) == 2
        # The request was built from the extracted document and carried the
        # case's Project Context, as the pipeline would send it.
        user = calls[0]["messages"][0]["content"]
        user = user if isinstance(user, str) else "".join(b["text"] for b in user)
        assert "Virginia" in user and "225 sq ft" in user
        score = me.score_review(me.load_records(out, me.BASELINE_ARM, ds.STAGE_REVIEW), cases)
        assert score["recall"]["count"] == 1 and score["recall"]["n"] == 2
        assert score["trap_hits"] == 1  # the electrical-room exception


# ===========================================================================
# 7. Scoring and the decision rules
# ===========================================================================


class TestStatistics:
    def test_wilson(self) -> None:
        assert me.wilson_interval(0, 0) is None
        assert me.wilson_interval(5, 10) == (0.2366, 0.7634)
        lo, hi = me.wilson_interval(0, 10)
        assert lo == 0.0 and 0.27 < hi < 0.28

    def test_exact_mcnemar(self) -> None:
        assert me.exact_mcnemar_p(0, 0) is None
        assert me.exact_mcnemar_p(0, 3) == 0.25
        assert me.exact_mcnemar_p(2, 2) == 1.0

    def test_percentile(self) -> None:
        assert me.percentile([], 50) is None
        assert me.percentile([1, 2, 3, 4], 50) == 2
        assert me.percentile([1, 2, 3, 4], 90) == 4


def _case(case_id):
    return next(c for c in ds.load_dataset() if c.case_id == case_id)


class TestVerificationOutcomes:
    def test_confirming_a_false_claim_is_a_false_confirmed(self) -> None:
        o = me.classify_verification_outcome(_case("v-nfpa13-metric-conversion-false"),
                                             {"status": "ok", "verdict": "CONFIRMED"})
        assert o["false_confirmed"] and not o["correct"] and not o["false_disputed"]

    def test_disputing_a_severe_true_finding_is_a_discard(self) -> None:
        o = me.classify_verification_outcome(_case("v-dsa-seismic-bracing-omitted"),
                                             {"status": "ok", "verdict": "DISPUTED"})
        assert o["false_disputed"] and o["discarded_severe_true"]

    def test_an_acceptable_verdict_is_not_an_error(self) -> None:
        o = me.classify_verification_outcome(_case("v-fm-ds-5-32-delete-as-noncode"),
                                             {"status": "ok", "verdict": "CORRECTED"})
        assert o["acceptable"] and not o["correct"] and not o["false_corrected"]

    def test_an_operational_failure_is_neither_right_nor_a_false_verdict(self) -> None:
        o = me.classify_verification_outcome(
            _case("v-nfpa13-oh-coverage-225"),
            {"status": "ok", "verdict": "UNVERIFIED", "verification_failed": True},
        )
        assert o["failed"] and not o["correct"] and not o["unwarranted_uncertainty"]

    def test_keeping_legitimate_uncertainty(self) -> None:
        o = me.classify_verification_outcome(_case("v-fictional-release-panel-approval"),
                                             {"status": "ok", "verdict": "UNVERIFIED"})
        assert o["correct"] and o["kept_uncertainty"]


def _vrecords(arm, verdicts, *, view=me.VIEW_TIER, rep=1, cost=0.01, latency=1.0, failed=()):
    return [
        {"arm_id": arm, "case_id": cid, "view": view, "repetition": rep, "status": "ok",
         "verdict": verdict, "verification_failed": cid in failed,
         "cost": {"usd": cost, "unknown_usage": 0}, "latency_seconds": latency}
        for cid, verdict in verdicts.items()
    ]


class TestDecisionRules:
    CASES = ds.load_dataset()

    def _truth(self):
        return {c.case_id: c.expected_verdict for c in ds.verification_cases(self.CASES, split=ds.SPLIT_HELD_OUT)}

    def _scores(self, base, cand, *, base_path=None, cand_path=None):
        cases = self.CASES
        out = {}
        for view, (b, c) in {me.VIEW_TIER: (base, cand), me.VIEW_PATH: (base_path or base, cand_path or cand)}.items():
            out[view] = {
                "baseline": me.score_verification(b, cases, view=view),
                "candidate": me.score_verification(c, cases, view=view),
                "paired": me.paired_verification(b, c, cases, view=view),
            }
        return me.decide(me.EXPERIMENT_ESCALATION,
                         baseline=out[me.VIEW_TIER]["baseline"], candidate=out[me.VIEW_TIER]["candidate"],
                         paired=out[me.VIEW_TIER]["paired"],
                         baseline_path=out[me.VIEW_PATH]["baseline"], candidate_path=out[me.VIEW_PATH]["candidate"],
                         paired_path=out[me.VIEW_PATH]["paired"])

    def _reps(self, arm, verdicts, view, reps=2, **kw):
        return [r for rep in range(1, reps + 1) for r in _vrecords(arm, verdicts, view=view, rep=rep, **kw)]

    def test_too_few_pairs_defers(self) -> None:
        truth = self._truth()
        base = _vrecords("baseline", truth)
        decision = self._scores(base, _vrecords("c", truth))
        assert decision["decision"] == me.DECISION_DEFER

    def test_a_new_false_disputed_rejects(self) -> None:
        truth = self._truth()
        worse = dict(truth, **{"v-dsa-seismic-bracing-omitted": "DISPUTED"})
        base = self._reps("b", truth, me.VIEW_TIER)
        cand = self._reps("c", worse, me.VIEW_TIER)
        decision = self._scores(base, cand, base_path=self._reps("b", truth, me.VIEW_PATH),
                                cand_path=self._reps("c", truth, me.VIEW_PATH))
        assert decision["decision"] == me.DECISION_REJECT
        assert any("false DISPUTED" in r for r in decision["reasons"])
        assert any("severe true finding" in r for r in decision["reasons"])

    def test_better_and_affordable_promotes(self) -> None:
        truth = self._truth()
        # The baseline gets three CONFIRMED truths wrong (UNVERIFIED, not a false
        # verdict); the candidate gets them right.
        wrong = [cid for cid, v in truth.items() if v == "CONFIRMED"][:3]
        base_v = dict(truth, **{cid: "UNVERIFIED" for cid in wrong})
        base = self._reps("b", base_v, me.VIEW_TIER)
        cand = self._reps("c", truth, me.VIEW_TIER)
        decision = self._scores(base, cand, base_path=self._reps("b", base_v, me.VIEW_PATH),
                                cand_path=self._reps("c", truth, me.VIEW_PATH, cost=0.011))
        assert decision["decision"] == me.DECISION_PROMOTE, decision

    def test_better_but_expensive_retains(self) -> None:
        truth = self._truth()
        wrong = [cid for cid, v in truth.items() if v == "CONFIRMED"][:3]
        base_v = dict(truth, **{cid: "UNVERIFIED" for cid in wrong})
        decision = self._scores(
            self._reps("b", base_v, me.VIEW_TIER), self._reps("c", truth, me.VIEW_TIER),
            base_path=self._reps("b", base_v, me.VIEW_PATH),
            cand_path=self._reps("c", truth, me.VIEW_PATH, cost=0.02),
        )
        assert decision["decision"] == me.DECISION_RETAIN
        assert any("cost ratio" in r for r in decision["reasons"])

    def test_no_gain_retains(self) -> None:
        truth = self._truth()
        both = self._reps("x", truth, me.VIEW_TIER)
        decision = self._scores(both, both, base_path=self._reps("x", truth, me.VIEW_PATH),
                                cand_path=self._reps("x", truth, me.VIEW_PATH))
        assert decision["decision"] == me.DECISION_RETAIN


def _review_record(case, found_labels, *, rep=1, extra=(), cost=0.1, parse_status="ok"):
    findings = []
    for d in case.defects:
        if d.label in found_labels:
            token = d.match_any[0]
            findings.append({"severity": d.severity, "issue": " ".join(token), "actionType": "REPORT_ONLY",
                             "confidence": 0.9})
    findings.extend(extra)
    return {"case_id": case.case_id, "repetition": rep, "status": "ok", "parse_status": parse_status,
            "findings": findings, "cost": {"usd": cost}, "latency_seconds": 1.0, "repair_attempts": 0}


class TestReviewScoring:
    CASES = ds.load_dataset()

    def _held(self):
        return ds.review_cases(self.CASES, split=ds.SPLIT_HELD_OUT)

    def _all(self, arm_found, reps=2, **kw):
        return [_review_record(c, arm_found(c), rep=rep, **kw) for rep in range(1, reps + 1) for c in self._held()]

    def test_trap_counts_only_edits(self) -> None:
        case = _case("r-dc-preaction-data-hall")
        note = {"issue": "Confirm the electrical room omission with the owner.", "actionType": "REPORT_ONLY"}
        edit = {"issue": "Add sprinklers to the electrical room.", "actionType": "EDIT"}
        assert me.match_review(case, [note])["trap_hits"] == []
        assert me.match_review(case, [edit])["trap_hits"] == [(case.traps[0].label, 0)]

    def test_adjudication_overrides_the_matcher(self) -> None:
        case = _case("r-dc-fire-pump")
        findings = [{"issue": "Head requirement is wrong.", "actionType": "REPORT_ONLY"}]
        assert me.match_review(case, findings)["matched"][case.defects[0].label] is None
        adj = {"matches": {case.defects[0].label: 0}, "unsupported": []}
        assert me.match_review(case, findings, adj)["matched"][case.defects[0].label] == 0

    def test_effort_rules(self) -> None:
        all_labels = lambda c: {d.label for d in c.defects}  # noqa: E731
        severe_missing = lambda c: {d.label for d in c.defects if not d.severe}  # noqa: E731
        base = self._all(severe_missing)
        cand = self._all(all_labels, cost=0.12)
        scores = {"baseline": me.score_review(base, self.CASES), "candidate": me.score_review(cand, self.CASES),
                  "paired": me.paired_review(base, cand, self.CASES)}
        assert me.decide(me.EXPERIMENT_REVIEW_EFFORT, **scores)["decision"] == me.DECISION_PROMOTE
        # The same gain at double the cost is retained.
        cand = self._all(all_labels, cost=0.2)
        scores["candidate"] = me.score_review(cand, self.CASES)
        assert me.decide(me.EXPERIMENT_REVIEW_EFFORT, **scores)["decision"] == me.DECISION_RETAIN
        # Losing severe defects rejects.
        scores = {"baseline": me.score_review(cand, self.CASES), "candidate": me.score_review(base, self.CASES),
                  "paired": me.paired_review(cand, base, self.CASES)}
        assert me.decide(me.EXPERIMENT_REVIEW_EFFORT, **scores)["decision"] == me.DECISION_REJECT

    def test_scope_rules_reject_more_trap_hits(self) -> None:
        all_labels = lambda c: {d.label for d in c.defects}  # noqa: E731
        trap_edit = {"issue": "Change NFPA 13 2019 to NFPA 13 2022.", "actionType": "EDIT"}
        base = self._all(all_labels)
        cand = self._all(all_labels, extra=(trap_edit,))
        scores = {"baseline": me.score_review(base, self.CASES), "candidate": me.score_review(cand, self.CASES),
                  "paired": me.paired_review(base, cand, self.CASES)}
        decision = me.decide(me.EXPERIMENT_REVIEW_SCOPE, **scores)
        assert decision["decision"] == me.DECISION_REJECT and any("trap" in r for r in decision["reasons"])

    def test_calibration_labels_only_known_findings(self) -> None:
        case = _case("r-dc-preaction-data-hall")
        rec = _review_record(case, {case.defects[0].label},
                             extra=({"issue": "Add sprinklers to the electrical room.", "actionType": "EDIT",
                                     "confidence": 0.95},
                                    {"issue": "Something else entirely.", "actionType": "REPORT_ONLY",
                                     "confidence": 0.3}))
        cal = me.score_review([rec], [case])["confidence_calibration"]
        assert cal["labelled_findings"] == 2
        assert cal["by_band"]["high"] == {"findings": 2, "supported": 1}


class TestScoreExperiment:
    def test_end_to_end_from_record_files(self, tmp_path) -> None:
        cases = ds.load_dataset()
        truth = {c.case_id: c.expected_verdict for c in ds.verification_cases(cases, split=ds.SPLIT_HELD_OUT)}
        for arm in ("baseline", "escalation_opus_4_8"):
            lines = []
            for rep in (1, 2):
                for view in me.VIEWS:
                    lines += _vrecords(arm, truth, view=view, rep=rep)
            path = tmp_path / f"{arm}.verification.r1.jsonl"
            path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
        result = me.score_experiment(me.EXPERIMENT_ESCALATION, tmp_path)
        assert result["decision"]["decision"] == me.DECISION_RETAIN  # identical arms: no gain
        assert result["scores"][me.VIEW_TIER]["paired"]["pairs"] == 2 * len(truth)
        assert result["dataset_sha256"] == ds.dataset_digest(cases)


class TestRunExperiment:
    def test_each_arm_runs_in_its_own_process_and_environment(self, tmp_path) -> None:
        calls = []

        def fake_runner(cmd, *, cwd, env, check):
            calls.append((cmd, env))
            return type("Done", (), {"returncode": 0})()

        me.run_experiment(me.EXPERIMENT_REVIEW_EFFORT, state_root=tmp_path / "state", out_dir=tmp_path / "out",
                          max_spend_usd=8.0, repetitions=2, live=True, runner=fake_runner)
        arms = [cmd[cmd.index("--arm") + 1] for cmd, _env in calls]
        assert arms == ["baseline", "review_effort_xhigh", "review_effort_xhigh", "baseline"]
        for cmd, env in calls:
            arm = me.ARMS[cmd[cmd.index("--arm") + 1]]
            assert me.environment_problems(arm, env) == []
            assert cmd[cmd.index("--max-spend-usd") + 1] == "2.0000"
        state_paths = {env["SPEC_CRITIC_CACHE_PATH"] for _cmd, env in calls}
        assert len(state_paths) == 4  # every (arm, repetition) has its own

    def test_refuses_without_live(self, tmp_path) -> None:
        with pytest.raises(me.RunRefused):
            me.run_experiment(me.EXPERIMENT_REVIEW_EFFORT, state_root=tmp_path, out_dir=tmp_path,
                              max_spend_usd=1.0, live=False)


class TestCli:
    def test_validate_is_clean(self, capsys) -> None:
        assert me.main(["validate"]) == 0
        assert json.loads(capsys.readouterr().out) == {"problems": []}

    def test_describe_says_not_run(self, capsys) -> None:
        assert me.main(["describe"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["evaluation_protocol"]["status"].startswith("NOT RUN")
        assert out["one_change_problems"] == [] and out["dataset_problems"] == []
        assert set(out["decision_rules"]) == set(me.EXPERIMENTS)
