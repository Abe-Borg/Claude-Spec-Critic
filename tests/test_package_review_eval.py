"""Hermetic integration and scoring checks for package prompt experiments."""
from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
from types import SimpleNamespace

import pytest

from evals import model_effort as me
from evals import package_review as pr
from evals import package_review_dataset as ds
from src.compliance import compliance_checker as compliance
from src.cross_check import cross_checker as cross
from src.core.api_config import MODEL_SONNET_55
from tests.fixtures.fake_anthropic import FakeMessage, FakeToolUseBlock, FakeUsage


def case(suffix: str):
    return next(c for c in ds.load_dataset() if c.case_id == f"tuning.{suffix}")


class ScriptedMessages:
    def __init__(self, route):
        self.route = route
        self.calls = []

    def stream(self, **params):
        self.calls.append(params)
        message = self.route(params)

        class Stream:
            text_stream = ()

            def __iter__(self):
                return iter(())

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get_final_message(self):
                if isinstance(message, Exception):
                    raise message
                return message

        return Stream()


def response(payload, *, stage="compliance", usage=None):
    return FakeMessage([FakeToolUseBlock(name=f"submit_{stage}_findings", input=payload)],
                       stop_reason="tool_use", model=MODEL_SONNET_55,
                       usage=FakeUsage() if usage is None else usage)


def wire(monkeypatch, route, cap=10):
    messages = ScriptedMessages(route)
    ledger = pr.AttemptLedger(cap)
    client = SimpleNamespace(messages=messages)
    for module, operation in ((compliance, "compliance"), (cross, "cross_check")):
        monkeypatch.setattr(module, "_get_client", lambda *a, operation=operation, **k:
                            pr.RecordedClient(client, ledger, operation))
        monkeypatch.setattr(module, "request_budget_for", lambda *a, **k: SimpleNamespace(fits=True))
    return messages, ledger


def coverage_payload(c, status, *, filename=None, findings=()):
    rid = c.requirements[0].item_id
    return {"compliance_summary": "Constructed response", "findings": list(findings),
            "coverage": [{"requirement_id": rid, "status": status, "reason": "Fixture evidence",
                          "spec_file": filename, "spec_section": "3.01" if filename else None}]}


def finding(c, *, issue=None, action="REPORT_ONLY"):
    return dict(severity="HIGH", fileName=c.specs[0].filename, section="3.01",
                issue=issue or f"{c.requirements[0].item_id}: missing owner test duration.",
                actionType=action, existingText="", replacementText="", codeReference="Supplied owner basis",
                confidence=0.4, anchorText=None, insertPosition=None)


def row(c, arm="baseline", *, findings=(), status="ok", repetition=1):
    return dict(arm_id=arm, case_id=c.case_id, repetition=repetition, split=c.split,
                status=status, findings=list(findings), coverage=[], attempts=[], latency_seconds=1.0)


def isolated_environment(monkeypatch, state):
    env = me.arm_environment(pr.ARMS["baseline"], state_dir=state)
    for key in tuple(os.environ):
        if key.startswith("SPEC_CRITIC_"):
            monkeypatch.delenv(key)
    for key, value in env.items():
        if key.startswith("SPEC_CRITIC_"):
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-hermetic-key")


def test_dataset_contract_and_limits():
    assert ds.validate_dataset() == []
    assert len(ds.load_dataset()) == 20
    assert {c.split for c in ds.load_dataset()} == {"tuning", "held_out"}
    assert ds.case_digest(case("cross.voltage")) != ds.case_digest(case("cross.clean"))
    assert ds.validate_dataset((replace(case("compliance.chunk_missing"), chunks=((0,), (0,))),))


def test_probe_changes_only_system_and_keeps_chunk_safeguards():
    cases = ds.load_dataset()
    probes = pr.comparison_probe(cases)
    assert len(probes["baseline"]) == 20
    c = case("compliance.chunk_represented")
    from src.modules.registry import get_module
    cycle = get_module(c.module_id).cycle
    shipped = compliance._compliance_system_prompt(cycle)
    with pr.prompt_arm("baseline"):
        base = compliance._compliance_system_prompt(cycle)
        assert "never use REPORT_ONLY to\nreport subset-local absence" in base
        assert "including\nzero" in base
        assert "not filter for importance or confidence" not in base
    assert compliance._compliance_system_prompt(cycle) == shipped


def test_prompt_fragment_drift_refuses_run(monkeypatch):
    original = cross._cross_system_prompt
    monkeypatch.setattr(cross, "_cross_system_prompt", lambda cycle: original(cycle).replace("silently lost", "lost"))
    with pytest.raises(me.RunRefused, match="fragment changed"):
        pr.request_probe("coverage", (case("cross.clean"),))


def test_production_cross_parser_via_adapter(monkeypatch):
    c = case("cross.voltage")
    f = dict(severity="HIGH", fileName=c.specs[0].filename, section="2.01",
             issue="FP-1 pump 480 V conflicts with controller 208 V.", actionType="REPORT_ONLY",
             existingText=c.specs[0].text.strip(), replacementText="", codeReference="Equipment data",
             confidence=0.8, affected_files=[s.filename for s in c.specs])
    messages, ledger = wire(monkeypatch, lambda _: response(
        {"coordination_summary": "Voltage conflict", "findings": [f]}, stage="cross_check"))
    with pr.prompt_arm("coverage"):
        result = pr.execute_case(c)
    assert result.cross_check_status == "completed"
    assert len(result.findings) == 1
    assert len(messages.calls) == len(ledger.attempts) == 1
    assert ledger.attempts[0]["operation"] == "cross_check"
    assert me.price_attempts(ledger.attempts)["usd"] > 0


def test_production_compliance_merge_drops_disproven_add(monkeypatch):
    c = case("compliance.chunk_represented")
    false_add = finding(c, action="ADD")
    false_add.update(anchorText=c.specs[1].text.strip(), insertPosition="after", replacementText=c.requirements[0].text)

    def route(params):
        if c.specs[0].text.strip() in params["messages"][0]["content"]:
            return response(coverage_payload(c, "represented", filename=c.specs[0].filename))
        return response(coverage_payload(c, "missing", findings=[false_add]))

    messages, ledger = wire(monkeypatch, route)
    result = pr.execute_case(c)
    assert len(messages.calls) == len(ledger.attempts) == 2
    assert result.cross_check_status == "completed"
    assert result.coverage[0]["status"] == "represented"
    assert result.findings == []
    assert all("one subset" in p["messages"][0]["content"] for p in messages.calls)
    assert len(ledger.responses) == 2  # raw tool payload retains the dropped ADD for audit


def test_report_only_subset_absence_is_visible_to_scoring(monkeypatch):
    c = case("compliance.chunk_represented")

    def route(params):
        present = c.specs[0].text.strip() in params["messages"][0]["content"]
        return response(coverage_payload(c, "represented" if present else "missing",
                        filename=c.specs[0].filename if present else None,
                        findings=[] if present else [finding(c)]))

    wire(monkeypatch, route)
    result = pr.execute_case(c)
    r = row(c, findings=[asdict(f) for f in result.findings])
    assert pr.classify(c, r)["dispositions"] == {"0": "unsupported"}


def test_budget_blocks_second_chunk_and_preserves_first_usage(monkeypatch):
    c = case("compliance.chunk_represented")
    messages, ledger = wire(monkeypatch, lambda _: response(coverage_payload(c, "represented",
                                  filename=c.specs[0].filename), usage=FakeUsage(input_tokens=100_000)), cap=0.01)
    result = pr.execute_case(c)
    assert len(messages.calls) == len(ledger.attempts) == 1
    assert result.chunk_failures == 1
    assert result.coverage_completeness.unassessed_specs
    assert "cap reached" in ledger.stop_reason()


@pytest.mark.parametrize("kind", ["error", "no_usage", "unpriced"])
def test_unknown_usage_and_unpriced_models_block_further_sends(kind):
    msg = response({"findings": [], "compliance_summary": "", "coverage": []})
    if kind == "error":
        msg = RuntimeError("stream interrupted")
    elif kind == "no_usage":
        msg.usage = None
    else:
        msg.model = "unpriced-fictional-model"
    messages = ScriptedMessages(lambda _: msg)
    ledger = pr.AttemptLedger(10)
    client = pr.RecordedClient(SimpleNamespace(messages=messages), ledger, "compliance")
    if kind == "error":
        with pytest.raises(RuntimeError):
            with client.stream(model=MODEL_SONNET_55) as stream:
                stream.get_final_message()
    else:
        with client.stream(model=MODEL_SONNET_55) as stream:
            stream.get_final_message()
            stream.get_final_message()  # accessing twice must not double-charge
    assert len(ledger.attempts) == 1
    with pytest.raises(me.RunRefused, match="unknown or unpriced"):
        with client.stream(model=MODEL_SONNET_55):
            pass
    assert len(messages.calls) == 1


def test_usage_survives_payload_parse_failure(monkeypatch):
    wire_messages, ledger = wire(monkeypatch, lambda _: FakeMessage([], model=MODEL_SONNET_55))
    result = pr.execute_case(case("compliance.clean"))
    assert result.cross_check_status == "failed"
    assert len(wire_messages.calls) == len(ledger.attempts) == 1
    assert me.price_attempts(ledger.attempts)["usd"] > 0


def test_scoring_distinguishes_matches_duplicates_and_unknowns():
    c = case("compliance.missing")
    r = row(c, findings=[finding(c), finding(c), finding(c, issue="An unexpected suggestion")])
    scored = pr.score((c,), [r])
    summary = scored["arms"]["baseline"]
    assert summary["recovered"] == summary["severe_recovered"] == 1
    assert summary["finding_counts"] == dict(supported=1, unsupported=0, duplicate=1, unclassified=1)
    assert summary["finding_precision"] is None
    assert summary["adjudicated_recall"] is None
    assert summary["total_pipeline_cost_usd"] is None
    assert scored["by_stage"]["compliance"]["baseline"]["recovered"] == 1
    assert scored["by_stage"]["cross_check"]["baseline"]["expected"] == 0


def test_digest_bound_adjudication_accepts_benign_advisory_and_penalizes_duplicate():
    c = case("compliance.missing")
    r = row(c, findings=[finding(c), finding(c), finding(c, issue="Supported optional advisory")])
    judgment = dict(findings_sha256=ds.digest(r["findings"]), reviewed=True,
                    matches={c.defects[0].label: 0}, dispositions={"1": "duplicate", "2": "supported"})
    summary = pr.score((c,), [r], {pr.record_key(r): judgment})["arms"]["baseline"]
    assert summary["finding_precision"] == pytest.approx(2 / 3)
    assert summary["adjudicated_severe_recall"] == 1
    judgment["findings_sha256"] = "stale"
    with pytest.raises(ValueError, match="Stale adjudication"):
        pr.classify(c, r, judgment)


@pytest.mark.parametrize("matches,dispositions", [
    ({"missing_test_duration": 4}, {}),
    ({"missing_test_duration": 0}, {"0": "supported"}),
    ({"missing_test_duration": None}, {}),
])
def test_invalid_adjudication_is_rejected(matches, dispositions):
    c = case("compliance.missing")
    r = row(c, findings=[finding(c)])
    judgment = dict(findings_sha256=ds.digest(r["findings"]), reviewed=True, matches=matches, dispositions=dispositions)
    with pytest.raises(ValueError):
        pr.classify(c, r, judgment)


def test_failure_counts_as_miss_and_incomplete_pair_is_marked():
    c = case("compliance.missing")
    base = row(c, findings=[finding(c)])
    candidate = row(c, "coverage", status="not_run")
    score = pr.score((c,), [base, candidate])
    assert score["arms"]["coverage"]["expected"] == 1
    assert score["arms"]["coverage"]["recovered"] == 0
    assert score["arms"]["coverage"]["coverage_expected"] == 1
    assert score["pairs"][0]["comparable"] is False


def test_non_controlling_advisory_is_unclassified_until_reviewed():
    c = case("compliance.non_controlling")
    rid = c.requirements[1].item_id
    advisory = finding(c, issue=f"{rid}: submit an RFI to confirm insurer witness preference.")
    r = row(c, findings=[advisory])
    assert pr.classify(c, r)["unclassified"] == ["0"]
    advisory["actionType"] = "EDIT"
    assert pr.classify(c, r)["dispositions"] == {"0": "unsupported"}


def write_experiment(tmp_path):
    cases = tuple(c for c in ds.load_dataset() if c.split == "tuning")
    probes = pr.comparison_probe(cases)
    manifest = dict(split="tuning", repetitions=1, dataset_sha256=ds.digest([asdict(c) for c in cases]),
                    source_sha256="fixed-source", probes=probes)
    (tmp_path / "experiment.json").write_text(json.dumps(manifest))
    for arm in pr.ARMS:
        rows = [{**row(c, arm), "case_sha256": ds.case_digest(c), "source_sha256": "fixed-source",
                 "request_probe": probes[arm][c.case_id], "requests": probes[arm][c.case_id]} for c in cases]
        pr.record_path(tmp_path, arm, 1).write_text("\n".join(json.dumps(r) for r in rows))
    return cases


@pytest.mark.parametrize("change", ["missing", "duplicate", "stale", "wrong_request", "unexpected_send"])
def test_records_must_form_a_complete_compatible_pair(tmp_path, change):
    write_experiment(tmp_path)
    assert len(pr.load_experiment(tmp_path)[1]) == 20
    path = pr.record_path(tmp_path, "coverage", 1)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if change == "missing":
        rows.pop()
    elif change == "duplicate":
        rows[0] = rows[1]
    elif change == "stale":
        rows[0]["case_sha256"] = "stale"
    elif change == "wrong_request":
        rows[0]["request_probe"][0]["other_sha256"] = "changed"
    else:
        rows[0]["requests"] = [{"model": "other", "system_sha256": "other", "other_sha256": "other"}]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    with pytest.raises(ValueError):
        pr.load_experiment(tmp_path)


@pytest.mark.parametrize("change", ["absent", "empty", "partial", "duplicate", "reversed", "extra"])
def test_successful_chunk_record_requires_the_complete_request_sequence(tmp_path, change):
    write_experiment(tmp_path)
    path = pr.record_path(tmp_path, "coverage", 1)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    record = next(r for r in rows if r["case_id"] == case("compliance.chunk_represented").case_id)
    first, second = record["requests"]
    if change == "absent":
        del record["requests"]
    else:
        record["requests"] = {
            "empty": [], "partial": [first], "duplicate": [first, first],
            "reversed": [second, first], "extra": [first, second, second],
        }[change]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    with pytest.raises(ValueError, match="request sequence"):
        pr.load_experiment(tmp_path)


@pytest.mark.parametrize("indexes", [(), (0,), (1,), (0, 1)])
def test_failed_chunk_record_accepts_requests_in_order_with_skips(tmp_path, indexes):
    write_experiment(tmp_path)
    path = pr.record_path(tmp_path, "coverage", 1)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    record = next(r for r in rows if r["case_id"] == case("compliance.chunk_represented").case_id)
    record.update(status="failed", requests=[record["request_probe"][i] for i in indexes])
    path.write_text("\n".join(json.dumps(r) for r in rows))
    cases, loaded = pr.load_experiment(tmp_path)
    scored = pr.score(cases, loaded)
    assert scored["arms"]["coverage"]["failed"] == 1
    assert next(p for p in scored["pairs"] if p["case_id"] == record["case_id"])["comparable"] is False


@pytest.mark.parametrize("indexes", [(0, 0), (1, 0), (0, 1, 1)])
def test_failed_chunk_record_rejects_duplicated_or_reordered_requests(tmp_path, indexes):
    write_experiment(tmp_path)
    path = pr.record_path(tmp_path, "coverage", 1)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    record = next(r for r in rows if r["case_id"] == case("compliance.chunk_represented").case_id)
    record.update(status="failed", requests=[record["request_probe"][i] for i in indexes])
    path.write_text("\n".join(json.dumps(r) for r in rows))
    with pytest.raises(ValueError, match="request sequence"):
        pr.load_experiment(tmp_path)


@pytest.mark.parametrize("sent", [False, True])
def test_unrun_record_cannot_have_sent_requests(tmp_path, sent):
    write_experiment(tmp_path)
    path = pr.record_path(tmp_path, "coverage", 1)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["status"] = "not_run"
    if not sent:
        del rows[0]["requests"]  # matches the collector's not_run output
    path.write_text("\n".join(json.dumps(r) for r in rows))
    if sent:
        with pytest.raises(ValueError, match="request sequence"):
            pr.load_experiment(tmp_path)
    else:
        assert len(pr.load_experiment(tmp_path)[1]) == 20


@pytest.mark.parametrize("requests", [None, {}, "not-a-sequence"])
def test_request_history_requires_a_list(tmp_path, requests):
    write_experiment(tmp_path)
    path = pr.record_path(tmp_path, "coverage", 1)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["requests"] = requests
    path.write_text("\n".join(json.dumps(r) for r in rows))
    with pytest.raises(ValueError, match="request sequence"):
        pr.load_experiment(tmp_path)


def test_collector_records_later_chunk_after_first_chunk_is_skipped(tmp_path, monkeypatch):
    c = case("compliance.chunk_represented")
    monkeypatch.setattr(ds, "load_dataset", lambda: (c,))
    write_experiment(tmp_path)
    for arm in pr.ARMS:
        pr.record_path(tmp_path, arm, 1).unlink()
    manifest = json.loads((tmp_path / "experiment.json").read_text())
    manifest["source_sha256"] = pr.source_digest()
    (tmp_path / "experiment.json").write_text(json.dumps(manifest))
    messages, _ledger = wire(monkeypatch, lambda _: response(coverage_payload(c, "missing")))
    original = compliance.run_compliance_check

    def run(specs, *args, **kwargs):
        if specs[0].filename == c.specs[0].filename:
            from src.review.reviewer import ReviewResult
            return ReviewResult(cross_check_status="skipped", thinking="Fixture budget skip.")
        return original(specs, *args, **kwargs)

    monkeypatch.setattr(compliance, "run_compliance_check", run)
    for arm in pr.ARMS:
        state = me.prepare_state_dir(tmp_path / "state", pr.ARMS[arm])
        isolated_environment(monkeypatch, state)
        pr.run_arm(arm, tmp_path, split="tuning", repetition=1, cap=10, live=True)
    _, records = pr.load_experiment(tmp_path)
    assert len(messages.calls) == 2
    assert all(r["status"] == "failed" and r["chunk_skips"] == 1 for r in records)
    assert all(r["requests"] == r["request_probe"][1:] for r in records)


@pytest.mark.parametrize("cap", [0, -1, float("nan"), float("inf")])
def test_invalid_spending_cap(cap):
    with pytest.raises(me.RunRefused):
        pr.AttemptLedger(cap)


def test_live_guard_refuses_before_any_request_or_output(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(me.RunRefused, match="--live"):
        pr.run_experiment(tmp_path / "out", split="tuning", repetitions=1, cap=10, live=False)
    with pytest.raises(me.RunRefused, match="API_KEY"):
        pr.run_experiment(tmp_path / "out", split="tuning", repetitions=1, cap=10, live=True)
    assert not (tmp_path / "out").exists()


def test_arm_environments_remove_operator_overrides_and_do_not_share_state(tmp_path):
    envs = []
    for arm in pr.ARMS.values():
        state = me.prepare_state_dir(tmp_path, arm)
        env = me.arm_environment(arm, state_dir=state,
            base_env={"SPEC_CRITIC_COMPLIANCE_EFFORT": "high", "SPEC_CRITIC_CACHE_PATH": "/operator/cache"})
        assert me.environment_problems(arm, env) == []
        assert "SPEC_CRITIC_COMPLIANCE_EFFORT" not in env
        envs.append(env)
    assert envs[0]["SPEC_CRITIC_CACHE_PATH"] != envs[1]["SPEC_CRITIC_CACHE_PATH"]


def test_run_arm_records_unknown_spend_then_marks_remaining_not_run(tmp_path, monkeypatch):
    cases = write_experiment(tmp_path)
    # Start with a fresh output slot; manifest remains required and binds source/probe.
    pr.record_path(tmp_path, "baseline", 1).unlink()
    manifest = json.loads((tmp_path / "experiment.json").read_text())
    manifest["source_sha256"] = pr.source_digest()
    (tmp_path / "experiment.json").write_text(json.dumps(manifest))
    state = me.prepare_state_dir(tmp_path / "state", pr.ARMS["baseline"])
    isolated_environment(monkeypatch, state)
    messages, _ledger = wire(monkeypatch, lambda _: RuntimeError("stream interrupted"))
    summary = pr.run_arm("baseline", tmp_path, split="tuning", repetition=1, cap=10, live=True)
    rows = [json.loads(line) for line in pr.record_path(tmp_path, "baseline", 1).read_text().splitlines()]
    assert len(rows) == len(cases)
    assert rows[0]["status"] == "failed"
    assert rows[0]["cost"]["unknown_usage"] == 1
    assert all(r["status"] == "not_run" for r in rows[1:])
    assert len(messages.calls) == 1
    assert summary["cost"]["unknown_usage"] == 1


def test_experiment_alternates_fresh_processes_and_strips_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-hermetic-key")
    monkeypatch.setenv("SPEC_CRITIC_COMPLIANCE_EFFORT", "high")
    calls = []

    def run(command, *, env, **kwargs):
        calls.append((command, env))
        if "probe" in command:
            return SimpleNamespace(stdout=json.dumps(pr.comparison_probe(
                tuple(c for c in ds.load_dataset() if c.split == "tuning"))))
        arm = command[command.index("--arm") + 1]
        rep = int(command[command.index("--repetition") + 1])
        summary = dict(arm_id=arm, repetition=rep, cost=dict(usd=0.1, unknown_usage=0, unpriced=0))
        return SimpleNamespace(returncode=0, stdout=json.dumps(summary), stderr="")

    monkeypatch.setattr(pr.subprocess, "run", run)
    result = pr.run_experiment(tmp_path / "out", split="tuning", repetitions=2, cap=10, live=True)
    assert [s["arm_id"] for s in result["runs"]] == ["baseline", "coverage", "coverage", "baseline"]
    assert all("SPEC_CRITIC_COMPLIANCE_EFFORT" not in env for _command, env in calls)
    paths = [env["SPEC_CRITIC_CACHE_PATH"] for command, env in calls if "_run-arm" in command]
    assert len(set(paths)) == 4
    assert all("--live" in command for command, _env in calls if "_run-arm" in command)


def test_overall_cap_stops_next_arm_after_one_request_overshoots(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-hermetic-key")
    sends = []

    def run(command, **kwargs):
        if "probe" in command:
            return SimpleNamespace(stdout=json.dumps(pr.comparison_probe(
                tuple(c for c in ds.load_dataset() if c.split == "tuning"))))
        sends.append(command)
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            dict(arm_id="baseline", repetition=1, cost=dict(usd=11, unknown_usage=0, unpriced=0))))

    monkeypatch.setattr(pr.subprocess, "run", run)
    with pytest.raises(me.RunRefused, match="spending cap reached"):
        pr.run_experiment(tmp_path / "out", split="tuning", repetitions=2, cap=10, live=True)
    assert len(sends) == 1
    assert (tmp_path / "out" / "runs.json").exists()


def test_arm_refuses_stale_source_before_any_send(tmp_path, monkeypatch):
    write_experiment(tmp_path)
    pr.record_path(tmp_path, "baseline", 1).unlink()
    state = me.prepare_state_dir(tmp_path / "state", pr.ARMS["baseline"])
    isolated_environment(monkeypatch, state)
    messages, _ledger = wire(monkeypatch, lambda _: RuntimeError("must not send"))
    with pytest.raises(me.RunRefused, match="source, dataset or request shape changed"):
        pr.run_arm("baseline", tmp_path, split="tuning", repetition=1, cap=10, live=True)
    assert messages.calls == []
    assert not pr.record_path(tmp_path, "baseline", 1).exists()
