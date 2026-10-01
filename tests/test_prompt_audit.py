"""Offline audit decisions, captured billing events, and evidence integrity."""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict, replace
import json
from types import SimpleNamespace

import pytest

from evals import model_effort as me
from evals import model_effort_dataset as ds
from evals import package_review as package
from evals import package_review_dataset as packages
from evals import prompt_audit as audit
from src.core.api_config import MODEL_HAIKU_45, MODEL_OPUS_55, MODEL_SONNET_55
from src.core.attempt_usage import known_attempt, unknown_attempt
from src.review.reviewer import Finding
from src.verification.verifier import VerificationResult
from tests.fixtures.fake_anthropic import FakeMessage, FakeServerToolUsage, FakeToolUseBlock, FakeUsage, sample_verification_verdict_payload
from tests.fixtures.verification_drivers import search_blocks
from tests.test_package_review_eval import ScriptedMessages


def synthetic_cases():
    module_id = ds.review_cases(ds.load_dataset())[0].module_id
    return tuple(ds.ReviewCase(f"audit-{n}", "held_out", ("severe_omission",), module_id,
        f"Audit {n}.docx", "1.01 SUMMARY\nA. Provide the specified equipment.", "Constructed test evidence",
        defects=tuple(ds.ExpectedDefect(f"defect-{n}-{i}", "HIGH", ((f"defect-{n}-{i}",),))
                      for i in range(2 if n == 0 else 0 if n == 7 else 1)), is_clean=n == 7)
        for n in range(8))


def manifest(cases, *, experiment=me.EXPERIMENT_REVIEW_HIGH, repetitions=3):
    probes = {}
    for arm in audit.arms(experiment):
        probes[arm.arm_id] = {"fixed": {}, "cases": {c.case_id: {
            "requests": [{"full_sha256": f"{arm.arm_id}/{c.case_id}/{i}", "model": MODEL_OPUS_55,
                          "system_sha256": "prompt", "fixed_sha256": "fixed", "effort": "medium"}
                         for i in range(2 if c.stage == "review" else len(package.partitions(c)))],
            "verification_context_sha256": "context"} for c in cases}}
    return {"experiment": experiment, "split": "held_out", "repetitions": repetitions,
            "declared_at": "2026-10-01T00:00:00+00:00", "probes": probes}


def finding(case, issue, severity="HIGH"):
    filename = getattr(case, "filename", None) or case.specs[0].filename
    return Finding(severity, filename, "1.01", issue, "REPORT_ONLY", "", "", "", confidence=0.9)


def seal(row):
    row["record_sha256"] = audit.digest({k: v for k, v in row.items() if k != "record_sha256"})
    return row


def row(case, m, arm="baseline", rep=1, *, objects=None, remote=False):
    objects = [finding(case, d.label) for d in case.defects] if objects is None else objects
    probe = m["probes"][arm]["cases"][case.case_id]
    requests = [{"operation": case.stage, "shape": probe["requests"][0]}]
    if case.stage != "review":
        requests = [{"operation": case.stage, "shape": shape} for shape in probe["requests"]]
    attempts = [known_attempt(FakeUsage(input_tokens=500, output_tokens=50), operation=case.stage,
                             transport="realtime", model=MODEL_OPUS_55).to_dict() for _ in requests]
    results = [VerificationResult("CONFIRMED", grounded=True, sources=["https://example.org/authority"],
        cache_status="miss" if remote else "local_skip") for _ in objects]
    if remote:
        for _ in objects:
            requests.append({"operation": "verification", "shape": {"full_sha256": "verifier", "model": MODEL_SONNET_55}})
            attempts.append(known_attempt(FakeUsage(input_tokens=500, output_tokens=50),
                operation="verification", transport="realtime", model=MODEL_SONNET_55).to_dict())
    return seal({"manifest_sha256": audit.digest(m), "case_id": case.case_id,
        "case_sha256": audit.digest(asdict(case)), "arm_id": arm, "repetition": rep, "stage": case.stage,
        "started_at": "2026-10-01T01:00:00+00:00", "request_probe": probe,
        "status": "ok", "findings": [asdict(f) for f in objects], "verifications": [asdict(v) for v in results],
        "report_statuses": ["VERIFIED_SUPPORTED" if remote else "LOCALLY_CLASSIFIED"] * len(objects),
        "attempts": attempts, "requests": requests, "responses": [[] for _ in attempts],
        "details": {"coverage": [{"requirement_id": rid, "status": status}
                                  for rid, status in getattr(case, "expected_coverage", {}).items()]},
        "review_latency_seconds": 0.2, "verification_latency_seconds": 0.7,
        "latency_seconds": 1.0, "cache_stats": {"hits": 0}})


def judge(case, r):
    matches = {d.label: next((i for i, f in enumerate(r["findings"]) if f["issue"] == d.label), None)
               for d in case.defects}
    used = {i for i in matches.values() if i is not None}
    dispositions = {str(i): "unsupported" for i in range(len(r["findings"])) if i not in used}
    retained = {i for i, status in enumerate(r["report_statuses"]) if status in audit.RETAINED_STATUSES}
    final_matches = {label: i if i in retained else None for label, i in matches.items()}
    return {"record_sha256": r["record_sha256"], "findings_sha256": audit.digest(r["findings"]),
            "reviewed": True, "matches": matches, "dispositions": dispositions,
            "final_matches": final_matches, "final_dispositions": {i: v for i, v in dispositions.items() if int(i) in retained}}


def study(cases=None, **kwargs):
    cases = synthetic_cases() if cases is None else cases
    m = manifest(cases, **kwargs)
    rows = [row(c, m, a.arm_id, rep) for a in audit.arms(m["experiment"])
            for rep in range(1, m["repetitions"] + 1) for c in cases]
    return m, cases, rows


def judgments(cases, rows):
    by_case = {c.case_id: c for c in cases}
    return {package.record_key(r): judge(by_case[r["case_id"]], r) for r in rows if r["status"] == "ok"}


def write_records(out, m, rows):
    out.mkdir(exist_ok=True)
    for arm in audit.arms(m["experiment"]):
        for rep in range(1, m["repetitions"] + 1):
            selected = [r for r in rows if r["arm_id"] == arm.arm_id and r["repetition"] == rep]
            audit.record_path(out, arm.arm_id, rep).write_text("".join(json.dumps(r) + "\n" for r in selected))


def test_fully_adjudicated_fixture_without_benefit_retains_defaults():
    m, cases, rows = study()
    result = audit.score(m, cases, rows, judgments(cases, rows))
    assert result["fixture_decision"]["decision"] == "retain"
    assert result["production_decision"]["decision"] == "defer"
    arms = result["stages"]["review"]["arms"]
    assert arms["baseline"]["final"]["severe_recovered"] == 24
    assert arms["baseline"]["final"]["precision"] == 1
    assert result["collection"]["baseline"]["cost_complete"]


def test_lower_review_cost_cannot_hide_higher_verification_cost():
    m, cases, rows = study()
    for r in rows:
        c = next(c for c in cases if c.case_id == r["case_id"])
        updated = row(c, m, r["arm_id"], r["repetition"], remote=True)
        if r["arm_id"] != "baseline":
            updated["attempts"][0]["input_tokens"] = 100
            for attempt in updated["attempts"][1:]:
                attempt["input_tokens"] = 100_000
        r.update(seal(updated))
    result = audit.score(m, cases, rows, judgments(cases, rows))
    stage = result["stages"]["review"]
    assert stage["fixture_decision"]["decision"] == "reject"
    assert "cost exceeded" in " ".join(stage["fixture_decision"]["reasons"])
    assert stage["arms"]["review_effort_high"]["cost_by_operation"]["review"]["usd"] < stage["arms"]["baseline"]["cost_by_operation"]["review"]["usd"]


def test_one_severe_loss_is_rejected_even_when_aggregate_recall_is_equal():
    cases = list(synthetic_cases())
    cases[6] = replace(cases[6], defects=cases[6].defects + (
        ds.ExpectedDefect("additional-high-a", "HIGH", (("additional-high-a",),)),
        ds.ExpectedDefect("additional-high-b", "HIGH", (("additional-high-b",),))))
    m, cases, rows = study(tuple(cases))
    for r in rows:
        c = next(c for c in cases if c.case_id == r["case_id"])
        # Baseline misses one defect, candidate misses a different defect.
        if (r["arm_id"] == "baseline" and c == cases[0]) or (r["arm_id"] != "baseline" and c == cases[1]):
            r.update(row(c, m, r["arm_id"], r["repetition"], objects=[finding(c, d.label) for d in c.defects[1:]]))
    result = audit.score(m, cases, rows, judgments(cases, rows))["stages"]["review"]
    assert result["arms"]["baseline"]["final"]["recovered"] == result["arms"]["review_effort_high"]["final"]["recovered"]
    assert result["fixture_decision"]["decision"] == "reject"
    assert len(result["severe_pair_losses"]) == 6
    assert result["arms"]["review_effort_high"]["final"]["severe_recall"] == 0.9
    assert not any("below floor" in reason for reason in result["fixture_decision"]["reasons"])


def test_genuine_fixture_recall_gain_is_only_a_fixture_adoption():
    m, cases, rows = study()
    for r in rows:
        if r["arm_id"] == "baseline" and r["case_id"] == cases[1].case_id:
            r.update(row(cases[1], m, r["arm_id"], r["repetition"], objects=[]))
    result = audit.score(m, cases, rows, judgments(cases, rows))
    assert result["fixture_decision"]["decision"] == "adopt"
    assert result["production_decision"]["decision"] == "defer"


@pytest.mark.parametrize("kind", ["unreviewed", "missing_judge", "missing_pair", "duplicate_pair", "failed", "unknown", "unpriced", "one_repetition", "tuning"])
def test_incomplete_or_insufficient_evidence_defers(kind):
    m, cases, rows = study(repetitions=1 if kind == "one_repetition" else 3)
    if kind == "missing_pair":
        rows.pop()
    elif kind == "duplicate_pair":
        rows[-1] = deepcopy(rows[-2])
    elif kind == "failed":
        rows[0]["status"] = "failed"
    elif kind == "unknown":
        rows[0]["attempts"][0] = unknown_attempt(operation="review", transport="realtime", model=MODEL_OPUS_55).to_dict()
    elif kind == "unpriced":
        rows[0]["attempts"][0]["model"] = "unpriced-model"
    elif kind == "tuning":
        m["split"] = "tuning"
    js = judgments(cases, rows)
    if kind == "missing_judge":
        js.pop(next(iter(js)))
    elif kind == "unreviewed":
        js[next(iter(js))]["reviewed"] = False
    result = audit.score(m, cases, rows, js)
    assert result["fixture_decision"]["decision"] == "defer"
    assert result["production_decision"]["decision"] == "defer"
    if kind in ("unknown", "unpriced", "failed", "missing_pair"):
        assert not all(s["cost_complete"] for s in result["collection"].values())


@pytest.mark.parametrize("kind", ["record", "findings", "flag", "invalid_index", "disputed_match", "missing_final_disposition"])
def test_adjudication_is_bound_to_full_record_and_final_outcomes(kind):
    m, cases, rows = study()
    r = rows[0]
    js = judgments(cases, rows)
    j = js[package.record_key(r)]
    if kind == "record":
        r["report_statuses"][0] = "INSUFFICIENT_EVIDENCE"
        seal(r)
    elif kind == "findings":
        j["findings_sha256"] = "stale"
    elif kind == "flag":
        j["reviewed"] = "true"
    elif kind == "invalid_index":
        j["final_matches"][cases[0].defects[0].label] = True
    elif kind == "disputed_match":
        r["report_statuses"][0] = "DISPUTED"
        seal(r)
        j["record_sha256"] = r["record_sha256"]
    else:
        j["final_matches"][cases[0].defects[0].label] = None
    with pytest.raises(ValueError):
        audit.score(m, cases, rows, js)


def test_template_starts_unreviewed_and_binds_verification_payload():
    m, cases, rows = study()
    template = audit.adjudication_template(cases, rows)
    entry = template[package.record_key(rows[0])]
    assert entry["reviewed"] is False
    assert entry["record_sha256"] == rows[0]["record_sha256"]
    assert all(i is None for i in entry["final_matches"].values())
    assert entry["final_dispositions"] == {"0": None, "1": None}


@pytest.mark.parametrize("kind", ["digest", "identity", "case", "before_declaration", "status", "event_count", "negative_tokens", "transport", "phase", "nan_latency", "phase_latency", "history", "first_repair", "revert_repair", "missing_verdict", "wrong_status", "unchecked", "missing_remote_call", "not_run_with_events", "duplicate_case"])
def test_loader_rejects_corrupted_or_incompatible_measurements(tmp_path, kind):
    m, cases, rows = study(repetitions=1)
    r = rows[0]
    if kind == "digest":
        r["latency_seconds"] = 2
    elif kind == "identity":
        r["arm_id"] = "wrong"
    elif kind == "case":
        r["case_sha256"] = "wrong"
    elif kind == "before_declaration":
        r["started_at"] = "2025-01-01T00:00:00+00:00"
    elif kind == "status":
        r["status"] = "success-ish"
    elif kind == "event_count":
        r["attempts"] = []
    elif kind == "negative_tokens":
        r["attempts"][0]["input_tokens"] = -100
    elif kind == "transport":
        r["attempts"][0]["transport"] = "batch"
    elif kind == "phase":
        r["attempts"][0]["operation"] = "research"
    elif kind == "nan_latency":
        r["latency_seconds"] = float("nan")
    elif kind == "phase_latency":
        r["verification_latency_seconds"] = 10
    elif kind == "history":
        r["requests"][0]["shape"] = {"full_sha256": "undeclared"}
    elif kind == "first_repair":
        r["requests"][0]["shape"] = r["request_probe"]["requests"][1]
    elif kind == "revert_repair":
        r["requests"].extend([{"operation": "review", "shape": shape} for shape in r["request_probe"]["requests"][::-1]])
        r["attempts"].extend([deepcopy(r["attempts"][0]), deepcopy(r["attempts"][0])])
        r["responses"].extend([[], []])
    elif kind == "missing_verdict":
        r["verifications"].pop()
    elif kind == "wrong_status":
        r["report_statuses"][0] = "VERIFIED_SUPPORTED"
    elif kind == "unchecked":
        r["report_statuses"][0] = "NOT_CHECKED"
    elif kind == "missing_remote_call":
        r["verifications"][0]["cache_status"] = "miss"
        r["report_statuses"][0] = "VERIFIED_SUPPORTED"
    elif kind == "not_run_with_events":
        r["status"] = "not_run"
    elif kind == "duplicate_case":
        rows[1] = deepcopy(rows[0])
    if kind != "digest":
        seal(r)
    write_records(tmp_path, m, rows)
    if kind == "identity":
        path = audit.record_path(tmp_path, "baseline", 1)
        path.write_text(json.dumps(r) + "\n" + path.read_text())
    with pytest.raises(ValueError):
        audit.load_records(tmp_path, m, cases)


def test_missing_files_and_failed_records_are_visible_and_do_not_become_zero_cost(tmp_path):
    m, cases, rows = study(repetitions=1)
    rows[0]["status"] = "failed"
    seal(rows[0])
    write_records(tmp_path, m, rows)
    audit.record_path(tmp_path, "review_effort_high", 1).unlink()
    records, problems = audit.load_records(tmp_path, m, cases)
    result = audit.score(m, cases, records, problems=problems)
    assert len(problems) == 2
    assert result["fixture_decision"]["decision"] == "defer"
    assert result["collection"]["baseline"]["cost"]["usd"] > 0
    assert not result["collection"]["review_effort_high"]["cost_complete"]


def test_package_stages_cannot_hide_each_others_regression():
    cases = audit.selected_cases(audit.PACKAGE_EXPERIMENT, "held_out")
    m, cases, rows = study(cases, experiment=audit.PACKAGE_EXPERIMENT)
    for r in rows:
        if r["arm_id"] == "coverage" and r["case_id"].endswith("compliance.clean"):
            r["details"]["coverage"][0]["status"] = "missing"
            seal(r)
    result = audit.score(m, cases, rows, judgments(cases, rows))
    assert result["stages"]["cross_check"]["fixture_decision"]["decision"] == "retain"
    assert result["stages"]["compliance"]["fixture_decision"]["decision"] == "reject"
    assert result["fixture_decision"]["decision"] == "reject"


def gate_summary():
    view = {"recall": 0.95, "severe_recall": 1.0, "precision": 0.98, "recovered": 20,
            "clean_false_positives": 0, "duplicate_rate": 0.0, "unsupported_severe": 0}
    return {"raw": deepcopy(view), "final": deepcopy(view), "cost": {"usd": 1.0},
            "p95_latency_seconds": 10.0, "coverage_errors": 0}


@pytest.mark.parametrize("field,value", [("recall", 0.94), ("severe_recall", 0.89), ("precision", 0.94),
    ("precision", 0.951), ("clean_false_positives", 1), ("duplicate_rate", 0.01), ("unsupported_severe", 1)])
def test_each_quality_gate_is_independent(field, value):
    baseline, candidate = gate_summary(), gate_summary()
    candidate["final"][field] = value
    assert audit.decision(baseline, candidate, severe_losses=[], sufficient=True)["decision"] == "reject"


@pytest.mark.parametrize("kind", ["cost", "latency", "zero_baseline_cost", "zero_baseline_latency"])
def test_resource_gates_include_total_cost_and_serial_latency(kind):
    baseline, candidate = gate_summary(), gate_summary()
    if kind == "cost":
        candidate["cost"]["usd"] = 1.251
    elif kind == "latency":
        candidate["p95_latency_seconds"] = 12.51
    elif kind == "zero_baseline_cost":
        baseline["cost"]["usd"] = 0
    else:
        baseline["p95_latency_seconds"] = 0
    expected = "defer" if kind.startswith("zero") else "reject"
    assert audit.decision(baseline, candidate, severe_losses=[], sufficient=True)["decision"] == expected


@pytest.mark.parametrize("ratio,expected", [(0.90, "adopt"), (0.901, "retain"), (1.25, "retain"), (1.251, "reject")])
def test_saving_and_cost_threshold_boundaries(ratio, expected):
    baseline, candidate = gate_summary(), gate_summary()
    candidate["cost"]["usd"] = ratio
    assert audit.decision(baseline, candidate, severe_losses=[], sufficient=True)["decision"] == expected


@pytest.mark.parametrize("kind", ["stream", "create"])
@pytest.mark.parametrize("outcome", ["ok", "exception", "no_usage", "unpriced"])
def test_billing_ledger_records_each_actual_sdk_call_and_stops_after_unknown(kind, outcome):
    msg = FakeMessage([], model=MODEL_SONNET_55, usage=FakeUsage(input_tokens=500, output_tokens=50))
    if outcome == "no_usage":
        msg.usage = None
    if outcome == "unpriced":
        msg.model = "unpriced-model"
    messages = ScriptedMessages(lambda _: RuntimeError("interrupted") if outcome == "exception" else msg)

    def create(**params):
        messages.calls.append(params)
        if outcome == "exception":
            raise RuntimeError("interrupted")
        return msg

    messages.create = create
    ledger = audit.Ledger(10)
    client = audit.Client(SimpleNamespace(messages=messages), ledger, "triage" if kind == "create" else "verification")
    def invoke():
        if kind == "create":
            return client.create(model=MODEL_SONNET_55)
        with client.stream(model=MODEL_SONNET_55) as stream:
            return stream.get_final_message()

    if outcome == "exception":
        with pytest.raises(RuntimeError):
            invoke()
    else:
        invoke()
    assert len(ledger.attempts) == len(ledger.request_events) == len(ledger.responses) == len(messages.calls) == 1
    if outcome != "ok":
        with pytest.raises(me.RunRefused):
            invoke()
        assert len(messages.calls) == 1
        assert len(ledger.request_events) == 1
    else:
        assert me.price_attempts(ledger.attempts)["usd"] > 0


@pytest.mark.parametrize("cap", [0, -1, float("inf"), float("nan")])
def test_invalid_budget_is_refused_before_call(cap):
    with pytest.raises(me.RunRefused):
        audit.Ledger(cap)


def test_all_production_client_factories_share_the_same_ledger(monkeypatch):
    from src.compliance import compliance_checker
    from src.cross_check import cross_checker
    from src.review import realtime_review
    from src.verification import triage, verifier

    modules = (realtime_review, cross_checker, compliance_checker, triage, verifier)
    messages = ScriptedMessages(lambda _: FakeMessage([], model=MODEL_SONNET_55))
    messages.create = lambda **kw: FakeMessage([], model=MODEL_SONNET_55)
    for module in modules:
        monkeypatch.setattr(module, "_get_client", lambda **kw: SimpleNamespace(messages=messages))
    ledger = audit.Ledger(10)
    with ExitStack() as stack:
        audit.recorded_clients(stack, ledger)
        for module in modules:
            client = module._get_client(sdk_retries=False)
            if module is triage:
                client.messages.create(model=MODEL_SONNET_55)
            else:
                with client.messages.stream(model=MODEL_SONNET_55) as stream:
                    stream.get_final_message()
    assert [a["operation"] for a in ledger.attempts] == ["review", "cross_check", "compliance", "triage", "verification"]


@pytest.mark.parametrize("sdk_model", [False, True])
def test_continuation_fingerprints_normalize_objects_without_changing_sent_messages(sdk_model):
    from anthropic.types import TextBlock
    from tests.fixtures.fake_anthropic import FakeTextBlock

    block = TextBlock(type="text", text="continued") if sdk_model else FakeTextBlock("continued")
    params = {"model": MODEL_SONNET_55, "messages": [{"role": "assistant", "content": [block]}]}
    plain = {"model": MODEL_SONNET_55, "messages": [{"role": "assistant", "content": [{"type": "text", "text": "continued"}]}]}
    if sdk_model:
        plain["messages"][0]["content"][0]["citations"] = None  # SDK's optional default field.
    assert audit.request_shape(params) == audit.request_shape(plain)
    assert package.request_shape(params) == package.request_shape(plain)
    messages = ScriptedMessages(lambda _: FakeMessage([], model=MODEL_SONNET_55))
    ledger = audit.Ledger(10)
    with ledger.stream(messages, "verification", **params) as stream:
        stream.get_final_message()
    assert messages.calls[0]["messages"][0]["content"][0] is block
    assert len(ledger.attempts) == 1


def test_verification_uses_real_prepass_and_escalation_entry_with_fresh_case_cache(monkeypatch):
    from src.verification import verifier

    calls = []
    def prepare(findings, **kwargs):
        calls.append(("prepare", kwargs))
        findings[0].verification = VerificationResult("UNVERIFIED", cache_status="local_skip")
        return findings[1:]

    def verify(finding, **kwargs):
        calls.append(("verify", kwargs))
        return VerificationResult("UNVERIFIED", grounded=False)

    monkeypatch.setattr(verifier, "prepare_findings_for_verification", prepare)
    monkeypatch.setattr(verifier, "verify_finding", verify)
    case = audit.selected_cases(audit.PACKAGE_EXPERIMENT, "held_out")[0]
    for _ in range(2):
        findings = [finding(case, "local"), finding(case, "remote")]
        audit.verify(findings, case)
    assert [name for name, _ in calls] == ["prepare", "verify", "prepare", "verify"]
    assert calls[0][1]["cache"] is calls[1][1]["cache"]
    assert calls[0][1]["cache"] is not calls[2][1]["cache"]
    assert "governing_basis" in calls[1][1]
    assert "escalated" not in calls[1][1]  # Production decides whether to escalate.


@pytest.mark.parametrize("pause", [False, True])
def test_collector_runs_production_package_parser_and_both_verifier_tiers(tmp_path, monkeypatch, pause):
    from src.cross_check import cross_checker
    from src.verification import verifier

    case = next(c for c in packages.load_dataset() if c.case_id == "held_out.cross.voltage")
    f = finding(case, "FP-7 pump 600 V conflicts with controller 240 V.")
    f.codeReference = "NFPA 20"
    f.existingText = case.specs[0].text.strip()
    f.affected_files = [s.filename for s in case.specs]
    turns = {}
    def route(params):
        names = {t["name"] for t in params["tools"]}
        if "submit_cross_check_findings" in names:
            return FakeMessage([FakeToolUseBlock("submit_cross_check_findings", {
                "coordination_summary": "Constructed voltage conflict", "findings": [audit.me._finding_dict(f) | {
                    "fileName": f.fileName, "affected_files": f.affected_files}]})],
                stop_reason="tool_use", model=params["model"])
        turns[params["model"]] = turns.get(params["model"], 0) + 1
        usage = FakeUsage(server_tool_use=FakeServerToolUsage(web_search_requests=1))
        if pause and turns[params["model"]] == 1:
            return FakeMessage(search_blocks(), stop_reason="pause_turn", model=params["model"], usage=usage)
        payload = sample_verification_verdict_payload()
        payload.update(verdict="UNVERIFIED", sources=[], source_quote=None,
                       explanation="The closed-world fixture cannot be settled on the web.")
        return FakeMessage([*search_blocks(), FakeToolUseBlock("submit_verification_verdict", payload)],
                           stop_reason="tool_use", model=params["model"], usage=usage)

    messages = ScriptedMessages(route)
    client = SimpleNamespace(messages=messages)
    monkeypatch.setattr(cross_checker, "_get_client", lambda **kw: client)
    monkeypatch.setattr(cross_checker, "request_budget_for", lambda *a, **kw: SimpleNamespace(fits=True))
    monkeypatch.setattr(verifier, "_get_client", lambda **kw: client)
    monkeypatch.setattr(me, "check_run_preconditions", lambda *a, **kw: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-hermetic-key")
    m = manifest((case,), experiment=audit.PACKAGE_EXPERIMENT, repetitions=1)
    m["probes"]["coverage"] = audit.arm_probe(audit.PACKAGE_EXPERIMENT, "coverage", "held_out")
    monkeypatch.setattr(audit, "load_manifest", lambda out: (m, (case,)))
    summary = audit.run_arm(tmp_path, arm_id="coverage", repetition=1, cap=10, live=True)
    captured = json.loads(audit.record_path(tmp_path, "coverage", 1).read_text())
    assert captured["status"] == "ok", captured["details"]
    turns_per_tier = 2 if pause else 1
    assert [a["operation"] for a in captured["attempts"]] == ["cross_check"] + ["verification"] * (2 * turns_per_tier)
    assert [a["model"] for a in captured["attempts"]] == [MODEL_SONNET_55] + [MODEL_SONNET_55] * turns_per_tier + [MODEL_OPUS_55] * turns_per_tier
    assert captured["verifications"][0]["escalation_attempted"]
    assert captured["report_statuses"] == ["INSUFFICIENT_EVIDENCE"]
    assert summary["cost"]["usd"] == me.price_attempts(captured["attempts"])["usd"] > 0
    assert len(messages.calls) == len(captured["requests"]) == 1 + 2 * turns_per_tier
    loaded, problems = audit.load_records(tmp_path, m, (case,))
    assert loaded == [captured]
    assert len(problems) == 1  # Baseline file is absent, so no paired decision.


@pytest.mark.parametrize("cap,status,calls", [(10, "ok", 2), (0.0001, "failed", 1)])
def test_collector_runs_real_review_and_triage_with_budget_checked_between_them(tmp_path, monkeypatch, cap, status, calls):
    from src.review import realtime_review
    from src.verification import triage

    case = audit.selected_cases(me.EXPERIMENT_REVIEW_HIGH, "held_out")[0]
    f = finding(case, "Equipment identifier differs between schedule and paragraph.", "MEDIUM")
    messages = ScriptedMessages(lambda params: FakeMessage([FakeToolUseBlock("submit_review_findings", {
        "analysis_summary": "Constructed review response", "findings": [me._finding_dict(f) | {"fileName": f.fileName}]})],
        model=params["model"], stop_reason="tool_use"))

    def create(**params):
        messages.calls.append(params)
        return FakeMessage([FakeToolUseBlock("submit_triage_classifications", {
            "classifications": [{"index": 0, "classification": "local_skip"}]})],
            model=params["model"], stop_reason="tool_use")

    messages.create = create
    client = SimpleNamespace(messages=messages)
    monkeypatch.setattr(realtime_review, "_get_client", lambda **kw: client)
    monkeypatch.setattr(realtime_review, "review_extended_output_count", lambda *a, **kw: 1000)
    monkeypatch.setattr(triage, "_get_client", lambda **kw: client)
    monkeypatch.setattr(me, "check_run_preconditions", lambda *a, **kw: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-hermetic-key")
    m = manifest((case,), repetitions=1)
    m["probes"]["baseline"] = audit.arm_probe(me.EXPERIMENT_REVIEW_HIGH, "baseline", "held_out")
    monkeypatch.setattr(audit, "load_manifest", lambda out: (m, (case,)))
    summary = audit.run_arm(tmp_path, arm_id="baseline", repetition=1, cap=cap, live=True)
    captured = json.loads(audit.record_path(tmp_path, "baseline", 1).read_text())
    assert captured["status"] == status, captured["details"]
    assert len(messages.calls) == len(captured["attempts"]) == calls
    assert summary["cost"]["usd"] > 0
    if calls == 2:
        assert [a["operation"] for a in captured["attempts"]] == ["review", "triage"]
        assert [a["model"] for a in captured["attempts"]] == [MODEL_OPUS_55, MODEL_HAIKU_45]
        assert captured["report_statuses"] == ["LOCALLY_CLASSIFIED"]
        loaded, problems = audit.load_records(tmp_path, m, (case,))
        assert loaded == [captured]
        assert len(problems) == 1
    else:
        assert "cap reached" in summary["stopped_reason"]


@pytest.mark.parametrize("kind", ["missing", "duplicate", "reordered"])
def test_successful_package_measurements_require_full_request_sequence(tmp_path, kind):
    case = next(c for c in packages.load_dataset() if c.case_id == "held_out.compliance.chunk_missing")
    m, cases, rows = study((case,), experiment=audit.PACKAGE_EXPERIMENT, repetitions=1)
    r = rows[0]
    if kind == "missing":
        r["requests"].pop()
        r["attempts"].pop()
        r["responses"].pop()
    elif kind == "duplicate":
        r["requests"][1] = deepcopy(r["requests"][0])
    else:
        r["requests"].reverse()
    seal(r)
    write_records(tmp_path, m, rows)
    with pytest.raises(ValueError, match="full ordered probe"):
        audit.load_records(tmp_path, m, cases)


def test_partially_written_arm_preserves_known_cost_and_defers(tmp_path):
    m, cases, rows = study(repetitions=1)
    write_records(tmp_path, m, rows[1:])
    records, problems = audit.load_records(tmp_path, m, cases)
    result = audit.score(m, cases, records, judgments(cases, records), problems=problems)
    assert result["fixture_decision"]["decision"] == "defer"
    assert result["collection"]["baseline"]["cost"]["usd"] > 0
    assert not result["collection"]["baseline"]["cost_complete"]


def test_verifier_cannot_override_a_fixed_unsafe_edit_trap():
    case = next(c for c in packages.load_dataset() if c.case_id == "held_out.compliance.non_controlling")
    m = manifest((case,), experiment=audit.PACKAGE_EXPERIMENT, repetitions=1)
    f = finding(case, case.requirements[1].item_id + ": mandatory insurer witness requirement.")
    f.actionType = "EDIT"
    r = row(case, m, objects=[f])
    j = judge(case, r)
    assert j["dispositions"] == {"0": "unsupported"}
    j["final_dispositions"]["0"] = "supported"
    with pytest.raises(ValueError, match="fixed traps"):
        audit.classifications(case, r, j)


@pytest.fixture(scope="module")
def declared_experiments(tmp_path_factory):
    result = {}
    for experiment in audit.EXPERIMENTS:
        out = tmp_path_factory.mktemp(experiment)
        result[experiment] = (out, audit.declare(out, experiment=experiment))
    return result


@pytest.mark.parametrize("experiment", audit.EXPERIMENTS)
def test_real_isolated_offline_declarations_cover_every_fixture(declared_experiments, experiment):
    out, m = declared_experiments[experiment]
    loaded, cases = audit.load_manifest(out)
    assert loaded["gates"] == audit.GATES
    assert len(cases) == (10 if experiment == audit.PACKAGE_EXPERIMENT else 8)
    assert set(loaded["probes"]) == {a.arm_id for a in audit.arms(experiment)}
    assert not list(out.glob("*.jsonl"))
    assert m["cost_scope"] == audit.COST_SCOPE


@pytest.mark.parametrize("field", ["gates", "dataset_sha256", "source_sha256", "runtime", "retained_statuses", "arms", "evidence_scope"])
def test_protocol_changes_after_declaration_are_refused(tmp_path, declared_experiments, field):
    _, original = declared_experiments[me.EXPERIMENT_REVIEW_HIGH]
    m = json.loads(json.dumps(original))
    m[field] = "changed"
    (tmp_path / "audit.json").write_text(json.dumps(m))
    with pytest.raises(ValueError):
        audit.load_manifest(tmp_path)


def test_live_collection_requires_both_live_flag_and_real_key(declared_experiments, monkeypatch):
    out, _ = declared_experiments[me.EXPERIMENT_REVIEW_HIGH]
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(me.RunRefused, match="real API key"):
        audit.run_experiment(out, cap=10, live=True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-hermetic-key")
    with pytest.raises(me.RunRefused, match="--live"):
        audit.run_experiment(out, cap=10, live=False)
    assert not (out / "collection.json").exists()


def test_missing_evidence_score_cli_defers_without_api_key(declared_experiments, monkeypatch, capsys):
    out, _ = declared_experiments[me.EXPERIMENT_REVIEW_HIGH]
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert audit.main(["score", "--out", str(out)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["fixture_decision"]["decision"] == "defer"
    assert result["production_decision"]["decision"] == "defer"
    assert len(result["fixture_decision"]["reasons"]) >= 6


def test_collection_alternates_fresh_processes_and_stops_on_unknown(tmp_path, declared_experiments, monkeypatch):
    _, original = declared_experiments[me.EXPERIMENT_REVIEW_HIGH]
    (tmp_path / "audit.json").write_text(json.dumps(original))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-hermetic-key")
    calls = []

    def runner(command, *, env, **kwargs):
        arm = command[command.index("--arm") + 1]
        repetition = int(command[command.index("--repetition") + 1])
        calls.append((arm, repetition, env))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"arm_id": arm, "repetition": repetition,
            "cost": {"usd": 0.1, "unknown_usage": int(len(calls) == 3), "unpriced": 0}}))

    monkeypatch.setattr(audit.subprocess, "run", runner)
    with pytest.raises(me.RunRefused, match="Unknown/unpriced"):
        audit.run_experiment(tmp_path, cap=10, live=True)
    assert [(a, r) for a, r, _ in calls] == [("baseline", 1), ("review_effort_high", 1), ("review_effort_high", 2)]
    assert len({env["SPEC_CRITIC_CACHE_PATH"] for _, _, env in calls}) == 3
    for arm, _, env in calls:
        assert me.environment_problems(me.ARMS[arm], env) == []
    assert len(json.loads((tmp_path / "runs.json").read_text())) == 3
    with pytest.raises(me.RunRefused, match="already started"):
        audit.run_experiment(tmp_path, cap=10, live=True)
