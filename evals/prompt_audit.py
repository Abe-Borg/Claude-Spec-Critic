"""Predeclared prompt-audit gates and captured review-plus-verification evidence.

All commands except ``run --live`` and its isolated ``_run-arm`` worker are
offline. Constructed fixtures can earn a fixture recommendation, never a
production adoption decision. Existing EX-03 decision rules stay untouched.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, nullcontext
from dataclasses import asdict, replace
from datetime import datetime, timezone
import importlib.metadata
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from evals import model_effort as me
from evals import model_effort_dataset as review_ds
from evals import package_review as package
from evals import package_review_dataset as package_ds


PACKAGE_EXPERIMENT = "package_coverage"
EXPERIMENTS = (me.EXPERIMENT_REVIEW_HIGH, me.EXPERIMENT_REVIEW_PROCEDURE,
               me.EXPERIMENT_REVIEW_SCOPE, PACKAGE_EXPERIMENT)
VERSION = 1
RETAINED_STATUSES = ("VERIFIED_SUPPORTED", "VERIFIED_CONTRADICTED", "LOCALLY_CLASSIFIED")
# Fixed before live collection, versioned with the source. These are operational
# screening thresholds, not statistical proof that a small fixture set generalizes.
GATES = {
    "minimum_repetitions": 3,
    "minimum_precision": 0.95,
    "maximum_precision_drop": 0.02,
    "minimum_severe_recall": 0.90,
    "maximum_cost_ratio": 1.25,
    "maximum_p95_latency_ratio": 1.25,
    "cost_saving_ratio": 0.90,
    "severe_pair_losses_allowed": 0,
    "recall_drop_allowed": 0,
    "clean_false_positive_increase_allowed": 0,
    "duplicate_rate_increase_allowed": 0,
    "retained_unsupported_severe_allowed": 0,
    "maximum_coverage_errors": 0,
}
MINIMUMS = {"review": {"cases": 8, "clean": 1, "severe": 8},
            "cross_check": {"cases": 4, "clean": 2, "severe": 1},
            "compliance": {"cases": 6, "clean": 3, "severe": 3}}
COST_SCOPE = "serial real-time review or package pass + triage + verification; excludes research and other stages"


def digest(value):
    return package_ds.digest(value)


def selected_cases(experiment, split):
    if experiment not in EXPERIMENTS or split not in review_ds.SPLITS:
        raise ValueError("Unknown audit experiment or split")
    if experiment == PACKAGE_EXPERIMENT:
        return tuple(c for c in package_ds.load_dataset() if c.split == split)
    return tuple(review_ds.review_cases(review_ds.load_dataset(), split=split))


def arms(experiment):
    if experiment == PACKAGE_EXPERIMENT:
        return tuple(package.ARMS.values())
    exp = me.EXPERIMENTS[experiment]
    return me.ARMS[exp.baseline], me.ARMS[exp.candidate]


def runtime_fingerprint():
    return {"python": sys.version.split()[0], **{
        name: importlib.metadata.version(name)
        for name in ("anthropic", "python-docx", "pydantic", "tiktoken")}}


def request_shape(params):
    # Continuations carry SDK content models, which the SDK accepts on the
    # wire. Normalize only the fingerprint copy; keep the sent request intact.
    params = json.loads(json.dumps(params, default=package._jsonable))
    fixed = {k: v for k, v in params.items() if k != "system"}
    if "output_config" in fixed:
        fixed["output_config"] = {k: v for k, v in fixed["output_config"].items() if k != "effort"}
    return {"full_sha256": digest(params), "system_sha256": digest(params.get("system")),
            "fixed_sha256": digest(fixed), "effort": params.get("output_config", {}).get("effort"),
            "model": params["model"]}


def review_jobs(case, directory):
    from src.core.api_config import REVIEW_MODEL_DEFAULT
    from src.modules.registry import get_module
    from src.review.realtime_review import build_realtime_review_jobs

    spec, alerts = me._prepared_review(case, spec_dir=directory)
    jobs, _ = build_realtime_review_jobs([spec], project_context=case.project_context,
        model=REVIEW_MODEL_DEFAULT, cycle=get_module(case.module_id).cycle,
        pre_detected_alerts={spec.filename: alerts})
    return jobs


def verification_context(case):
    from src.core.project_profile import ProjectProfile
    from src.modules.registry import get_module
    from src.orchestration.pipeline import build_run_governing_basis

    project = getattr(case, "project", {})
    location, fingerprint, _ = me._profile_inputs(project)
    profile = package.prepared_case(case)[1].to_dict() if isinstance(case, package_ds.PackageCase) else None
    basis = build_run_governing_basis(module=get_module(case.module_id),
        project_profile=ProjectProfile(**project) if project else None, requirements_profile=profile)
    return {"user_location": location, "jurisdiction_fingerprint": fingerprint, "governing_basis": basis}


def case_probe(case, directory):
    from src.compliance.compliance_checker import build_compliance_request
    from src.core.chunked_pass import filter_findings_for_chunk
    from src.cross_check.cross_checker import build_cross_check_request
    from src.modules.registry import get_module
    from src.review.realtime_review import RETRY_TRUNCATED_REVIEW_INSTRUCTION
    from src.review.review_request_builder import build_review_request

    if isinstance(case, review_ds.ReviewCase):
        jobs = review_jobs(case, directory)
        if len(jobs) != 1:
            raise me.RunRefused("Audit review fixtures must produce one review job")
        spec = jobs[0].request_spec
        requests = [request_shape(build_review_request(spec).params),
                    request_shape(build_review_request(replace(spec,
                        retry_instruction=RETRY_TRUNCATED_REVIEW_INSTRUCTION)).params)]
    else:
        specs, profile, existing = package.prepared_case(case)
        requests = []
        for indexes in package.partitions(case):
            subset = [specs[i] for i in indexes]
            prior = filter_findings_for_chunk(existing, {s.filename for s in subset}) if case.chunks else existing
            kwargs = dict(project_context=package_ds.PROJECT_CONTEXT,
                          cycle=get_module(case.module_id).cycle, chunk_subset=bool(case.chunks))
            params = (build_cross_check_request(subset, prior, **kwargs) if case.stage == "cross_check"
                      else build_compliance_request(subset, profile, prior, **kwargs))
            requests.append(request_shape(params))
    return {"requests": requests, "verification_context_sha256": digest(verification_context(case))}


def fixed_control_probe():
    from src.core import api_config
    from src.modules.registry import AVAILABLE_MODULES, get_module
    from src.review.reviewer import Finding
    from src.review.structured_schemas import triage_classifications_tool, triage_tool_choice
    from src.verification import triage, verifier
    from src.verification.verification_routing import build_verification_request, select_routing

    fixed = me.request_probe()
    requests = {}
    for module_id in sorted(AVAILABLE_MODULES):
        cycle = get_module(module_id).cycle
        for severity in ("GRIPES", "HIGH", "CRITICAL"):
            finding = Finding(severity, "21 13 13.docx", "3.01", "Check sprinkler protection area.",
                              "REPORT_ONLY", None, None, "NFPA 13")
            for escalated in (False, True):
                for phase in (api_config.PHASE_VERIFICATION, api_config.PHASE_VERIFICATION_RETRY,
                              api_config.PHASE_VERIFICATION_CONTINUATION):
                    decision = select_routing(finding, cycle=cycle, escalated=escalated,
                                              local_skip=False, cache_phase=phase)
                    built = build_verification_request(decision,
                        prompt=verifier._build_verification_prompt(finding, cycle=cycle,
                            include_verdict_tool=decision.include_verdict_tool),
                        system_prompt=verifier._get_verification_system_prompt(cycle,
                            include_verdict_tool=decision.include_verdict_tool))
                    requests[f"{module_id}/{severity}/{escalated}/{phase}"] = {
                        "params": built.params, "headers": built.extra_headers}
    fixed["verification.full_requests_sha256"] = digest(requests)
    finding = Finding("MEDIUM", "21 13 13.docx", "3.01", "Check equipment tags against quoted text.",
                      "REPORT_ONLY", "Tag FP-1", None, "")
    model = api_config.TRIAGE_MODEL_DEFAULT
    fixed["triage.request_sha256"] = digest({"model": model,
        "max_tokens": api_config.triage_max_tokens(model=model),
        "system": api_config.system_prompt_with_cache(triage._TRIAGE_SYSTEM_PROMPT, phase=api_config.PHASE_TRIAGE),
        "tools": api_config.tools_with_cache([triage_classifications_tool(model=model)], phase=api_config.PHASE_TRIAGE),
        "tool_choice": triage_tool_choice(model=model),
        "messages": [{"role": "user", "content": triage._build_user_prompt([(0, finding)])}]})
    return fixed


def arm_probe(experiment, arm_id, split):
    context = package.prompt_arm(arm_id) if experiment == PACKAGE_EXPERIMENT else nullcontext()
    with context, tempfile.TemporaryDirectory(prefix="spec-critic-audit-probe-") as directory:
        return {"fixed": fixed_control_probe(), "cases": {
            c.case_id: case_probe(c, Path(directory)) for c in selected_cases(experiment, split)}}


def validate_probes(experiment, probes):
    baseline, candidate = arms(experiment)
    base, other = probes[baseline.arm_id], probes[candidate.arm_id]
    expected = (["compliance_request.sha256", "cross_check_request.sha256"]
                if experiment == PACKAGE_EXPERIMENT else sorted(candidate.expected_request_changes))
    if me.probe_differences(base["fixed"], other["fixed"]) != expected:
        raise me.RunRefused("Fixed request controls changed outside this experiment")
    if set(base["cases"]) != set(other["cases"]):
        raise me.RunRefused("Probe case membership differs")
    for case_id, before in base["cases"].items():
        after = other["cases"][case_id]
        if before["verification_context_sha256"] != after["verification_context_sha256"]:
            raise me.RunRefused("Verification context changed between arms")
        if len(before["requests"]) != len(after["requests"]):
            raise me.RunRefused("Request partitions changed between arms")
        field = "effort" if experiment == me.EXPERIMENT_REVIEW_HIGH else "system_sha256"
        for left, right in zip(before["requests"], after["requests"], strict=True):
            changes = me.probe_differences(left, right)
            if changes != sorted([field, "full_sha256"]):
                raise me.RunRefused(f"{case_id}: expected only the declared request change")


def declare(out, *, experiment, split="held_out", repetitions=3):
    """Freeze gates, source, runtime, dataset and isolated probes before outputs."""
    if type(repetitions) is not int or repetitions < 1:
        raise me.RunRefused("Positive integer repetitions required")
    if out.exists() and any(out.iterdir()):
        raise me.RunRefused("Declaration requires an empty output directory")
    cases = selected_cases(experiment, split)
    problems = package_ds.validate_dataset() if experiment == PACKAGE_EXPERIMENT else review_ds.validate_dataset()
    if problems or not cases:
        raise me.RunRefused("Invalid or empty audit dataset")
    out.mkdir(parents=True, exist_ok=True)
    probes = {}
    for arm in arms(experiment):
        state = me.prepare_state_dir(out / "probe-state", arm)
        proc = subprocess.run([sys.executable, "-m", "evals.prompt_audit", "_probe",
            "--experiment", experiment, "--arm", arm.arm_id, "--split", split],
            cwd=str(review_ds._REPO_ROOT), env=me.arm_environment(arm, state_dir=state),
            capture_output=True, text=True, timeout=120, check=True)
        probes[arm.arm_id] = json.loads(proc.stdout)
    validate_probes(experiment, probes)
    manifest = {"version": VERSION, "experiment": experiment, "split": split,
        "repetitions": repetitions, "arms": [asdict(a) for a in arms(experiment)],
        "gates": GATES, "minimums": MINIMUMS, "retained_statuses": RETAINED_STATUSES,
        "declared_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": package.source_digest(), "runtime": runtime_fingerprint(),
        "dataset_sha256": digest([asdict(c) for c in cases]), "probes": probes,
        "cost_scope": COST_SCOPE, "evidence_scope": "constructed fixtures"}
    (out / "audit.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def load_manifest(out):
    m = json.loads((out / "audit.json").read_text(encoding="utf-8"))
    cases = selected_cases(m["experiment"], m["split"])
    if (m["version"] != VERSION or m["gates"] != GATES or m["minimums"] != MINIMUMS
        or m["retained_statuses"] != list(RETAINED_STATUSES) or m["cost_scope"] != COST_SCOPE
        or m["evidence_scope"] != "constructed fixtures"
        or m["arms"] != json.loads(json.dumps([asdict(a) for a in arms(m["experiment"])]))
        or type(m["repetitions"]) is not int or m["repetitions"] < 1
        or m["source_sha256"] != package.source_digest() or m["runtime"] != runtime_fingerprint()
        or m["dataset_sha256"] != digest([asdict(c) for c in cases])):
        raise ValueError("Audit protocol, source, runtime or dataset changed; use its original checkout")
    if set(m["probes"]) != {a.arm_id for a in arms(m["experiment"])}:
        raise ValueError("Invalid probe arm membership")
    for probe in m["probes"].values():
        if set(probe["cases"]) != {c.case_id for c in cases}:
            raise ValueError("Invalid probe case membership")
    validate_probes(m["experiment"], m["probes"])
    datetime.fromisoformat(m["declared_at"])
    return m, cases


class Ledger(package.AttemptLedger):
    """One billing event per actual SDK request, including triage and failures."""
    def __init__(self, cap):
        super().__init__(cap)
        self.request_events = []

    def before(self, operation, params):
        if reason := self.stop_reason():
            raise me.RunRefused(reason)
        self.request_events.append({"operation": operation, "shape": request_shape(params)})

    def stream(self, delegate, operation, **params):
        self.before(operation, params)
        return super().stream(delegate, operation, **params)

    def create(self, delegate, operation, **params):
        from src.core.attempt_usage import known_attempt, unknown_attempt

        self.before(operation, params)
        self.requests.append(package.request_shape(params))
        try:
            message = delegate.create(**params)
        except Exception:
            self.attempts.append(unknown_attempt(operation=operation, transport="realtime",
                model=params["model"], outcome="create_failed").to_dict())
            self.responses.append(None)
            raise
        usage = getattr(message, "usage", None)
        kwargs = dict(operation=operation, transport="realtime", model=getattr(message, "model", None) or params["model"])
        attempt = known_attempt(usage, message_id=getattr(message, "id", ""), **kwargs) if usage is not None else unknown_attempt(**kwargs)
        self.attempts.append(attempt.to_dict())
        self.responses.append(json.loads(json.dumps(getattr(message, "content", []), default=package._jsonable)))
        return message


class Client(package.RecordedClient):
    def create(self, **params):
        return self.ledger.create(self.client.messages, self.operation, **params)


def recorded_clients(stack, ledger):
    from src.compliance import compliance_checker
    from src.cross_check import cross_checker
    from src.review import realtime_review
    from src.verification import triage, verifier

    for module, operation in ((realtime_review, "review"), (cross_checker, "cross_check"),
            (compliance_checker, "compliance"), (triage, "triage"), (verifier, "verification")):
        factory = module._get_client

        def wrapped(*args, factory=factory, operation=operation, **kwargs):
            client = factory(*args, **kwargs)
            if hasattr(client, "close"):
                stack.callback(client.close)
            return Client(client, ledger, operation)

        stack.enter_context(patch.object(module, "_get_client", wrapped))


def generate(case, directory):
    if isinstance(case, package_ds.PackageCase):
        result = package.execute_case(case)
        ok = (result.cross_check_status == "completed" and not result.error
              and not result.chunk_failures and not result.chunk_skips)
        return result.findings, ok, {"error": result.error, "parse_status": result.parse_status,
            "chunk_failures": result.chunk_failures, "chunk_skips": result.chunk_skips, "coverage": result.coverage}
    from src.review.realtime_review import run_realtime_review_jobs

    jobs = review_jobs(case, directory)
    result = run_realtime_review_jobs(jobs, max_workers=1)[jobs[0].job_key]
    return result.findings, result.parse_status == "ok" and not result.error, {
        "error": result.error, "parse_status": result.parse_status}


def verify(findings, case):
    """Production pre-pass and escalation path, fresh cache per case, serial calls."""
    from src.modules.registry import get_module
    from src.verification.verification_cache import VerificationCache
    from src.verification.verifier import prepare_findings_for_verification, verify_finding

    cache = VerificationCache()
    context = verification_context(case)
    kwargs = dict(cycle=get_module(case.module_id).cycle, cache=cache, **context)
    remaining = prepare_findings_for_verification(findings, **{k: v for k, v in kwargs.items() if k != "user_location"})
    for finding in remaining:
        finding.verification = verify_finding(finding, **kwargs)
    return cache.stats()


def record_path(out, arm_id, repetition):
    return out / f"{arm_id}.audit.r{repetition}.jsonl"


def run_arm(out, *, arm_id, repetition, cap, live):
    from src.output.report_status import classify_status

    m, cases = load_manifest(out)
    arm = next(a for a in arms(m["experiment"]) if a.arm_id == arm_id)
    if type(repetition) is not int or not 1 <= repetition <= m["repetitions"]:
        raise me.RunRefused("Repetition outside the declaration")
    me.check_run_preconditions(arm, out_dir=out, stage="audit", repetition=repetition,
                              live=live, max_spend_usd=cap)
    ledger = Ledger(cap)
    probe = arm_probe(m["experiment"], arm_id, m["split"])
    if probe != m["probes"][arm_id]:
        raise me.RunRefused("Request controls changed before this arm; no request sent")
    context = package.prompt_arm(arm_id) if m["experiment"] == PACKAGE_EXPERIMENT else nullcontext()
    with context, ExitStack() as stack, record_path(out, arm_id, repetition).open("x", encoding="utf-8") as fh:
        recorded_clients(stack, ledger)
        for case in cases:
            started = datetime.now(timezone.utc).isoformat()
            base = {"manifest_sha256": digest(m), "case_id": case.case_id, "case_sha256": digest(asdict(case)),
                "arm_id": arm_id, "repetition": repetition, "stage": case.stage, "started_at": started,
                "request_probe": probe["cases"][case.case_id]}
            if reason := ledger.stop_reason():
                fh.write(json.dumps({**base, "status": "not_run", "reason": reason}) + "\n")
                fh.flush()
                continue
            offset = len(ledger.attempts)
            start = time.monotonic()
            phase = start
            findings, verifications, statuses, details, cache_stats = [], [], [], {}, {}
            review_latency = verification_latency = None
            status = "failed"
            try:
                objects, ok, details = generate(case, out / "specs" / arm_id / f"r{repetition}")
                review_latency = time.monotonic() - phase
                findings = [asdict(f) for f in objects]
                if ok:
                    phase = time.monotonic()
                    cache_stats = verify(objects, case)
                    verification_latency = time.monotonic() - phase
                    verifications = [asdict(f.verification) if f.verification is not None else None for f in objects]
                    statuses = [classify_status(f).value for f in objects]
                    status = "ok" if all(v is not None and not v["verification_failed"] for v in verifications) else "failed"
            except Exception as exc:
                details["error"] = f"{type(exc).__name__}: {exc}"
            row = {**base, "status": status, "details": details, "findings": findings,
                "verifications": verifications, "report_statuses": statuses, "cache_stats": cache_stats,
                "requests": ledger.request_events[offset:], "responses": ledger.responses[offset:],
                "attempts": ledger.attempts[offset:], "review_latency_seconds": review_latency,
                "verification_latency_seconds": verification_latency,
                "latency_seconds": time.monotonic() - start}
            row["record_sha256"] = digest(row)
            fh.write(json.dumps(row, ensure_ascii=False, default=package._jsonable) + "\n")
            fh.flush()
    return {"arm_id": arm_id, "repetition": repetition,
            "cost": me.price_attempts(ledger.attempts), "stopped_reason": ledger.stop_reason()}


def run_experiment(out, *, cap, live):
    m, _ = load_manifest(out)
    Ledger(cap)
    if not live or os.environ.get("ANTHROPIC_API_KEY", "") in ("", me._SENTINEL_KEY):
        raise me.RunRefused("Paid collection requires --live, a real API key and a finite positive cap")
    if (out / "collection.json").exists() or any(out.glob("*.jsonl")) or (out / "state").exists():
        raise me.RunRefused("This declaration has already started collection; declare a fresh directory")
    collection = {"manifest_sha256": digest(m), "cap_usd": cap,
                  "started_at": datetime.now(timezone.utc).isoformat()}
    (out / "collection.json").write_text(json.dumps(collection, indent=2), encoding="utf-8")
    summaries = []
    allocation = cap / (2 * m["repetitions"])
    for repetition in range(1, m["repetitions"] + 1):
        order = arms(m["experiment"])[::1 if repetition % 2 else -1]
        for arm in order:
            remaining = cap - sum(s["cost"]["usd"] for s in summaries)
            if remaining <= 0:
                raise me.RunRefused("Collection cap reached; partial records retained")
            state = me.prepare_state_dir(out / "state" / f"r{repetition}", arm)
            proc = subprocess.run([sys.executable, "-m", "evals.prompt_audit", "_run-arm",
                "--out", str(out.resolve()), "--arm", arm.arm_id, "--repetition", str(repetition),
                "--cap-usd", str(min(allocation, remaining)), "--live"],
                cwd=str(review_ds._REPO_ROOT), env=me.arm_environment(arm, state_dir=state),
                capture_output=True, text=True, check=False)
            if proc.returncode:
                raise me.RunRefused("An isolated arm failed; no later arm started; partial records retained")
            summary = json.loads(proc.stdout)
            summaries.append(summary)
            (out / "runs.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
            if summary["cost"]["unknown_usage"] or summary["cost"]["unpriced"]:
                raise me.RunRefused("Unknown/unpriced usage; no later arm started")
    return {"runs": summaries, "cost_scope": COST_SCOPE}


def load_records(out, m, cases):
    from src.core.attempt_usage import AttemptUsage
    from src.output.report_status import classify_status
    from src.review.reviewer import Finding
    from src.verification.verifier import VerificationResult

    by_case = {c.case_id: c for c in cases}
    rows, problems = [], []
    for arm in arms(m["experiment"]):
        for rep in range(1, m["repetitions"] + 1):
            path = record_path(out, arm.arm_id, rep)
            if not path.exists():
                problems.append(f"Missing {path.name}")
                continue
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            case_ids = [r["case_id"] for r in records]
            if len(case_ids) != len(set(case_ids)) or set(case_ids) - set(by_case):
                raise ValueError("Duplicate or extra cases in an arm/repetition file")
            if set(case_ids) != set(by_case):
                problems.append(f"Missing cases in {path.name}")
            for row in records:
                case = by_case[row["case_id"]]
                if (row["manifest_sha256"] != digest(m) or row["case_sha256"] != digest(asdict(case))
                    or row["arm_id"] != arm.arm_id or row["repetition"] != rep or row["stage"] != case.stage
                    or row["request_probe"] != m["probes"][arm.arm_id]["cases"][case.case_id]
                    or datetime.fromisoformat(row["started_at"]) < datetime.fromisoformat(m["declared_at"])):
                    raise ValueError("Incompatible record identity, declaration or request controls")
                if row["status"] not in ("ok", "failed", "not_run"):
                    raise ValueError("Invalid audit record status")
                if row["status"] == "not_run":
                    if any(row.get(k) for k in ("attempts", "requests", "responses", "findings", "verifications")):
                        raise ValueError("An unrun record carries measured events")
                    problems.append(f"Unrun {package.record_key(row)}")
                    rows.append(row)
                    continue
                if row["record_sha256"] != digest({k: v for k, v in row.items() if k != "record_sha256"}):
                    raise ValueError("Record digest differs from its captured contents")
                if len(row["attempts"]) != len(row["requests"]) or len(row["responses"]) != len(row["attempts"]):
                    raise ValueError("Attempt/request/response event counts differ")
                for raw, request in zip(row["attempts"], row["requests"], strict=True):
                    if (raw != AttemptUsage.from_dict(raw).to_dict() or raw["transport"] != "realtime"
                        or raw["operation"] != request["operation"]
                        or raw["operation"] not in (case.stage, "triage", "verification")):
                        raise ValueError("Invalid attempt counters, transport or operation")
                latency = row["latency_seconds"]
                if type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0:
                    raise ValueError("Invalid pipeline latency")
                if row["status"] != "ok":
                    problems.append(f"Failed {package.record_key(row)}")
                    rows.append(row)
                    continue
                observed = [r["shape"] for r in row["requests"] if r["operation"] == case.stage]
                expected = row["request_probe"]["requests"]
                if case.stage == "review":
                    # Production retries repeat a primary or repair; repair may
                    # follow primary, but may not precede it or revert to primary.
                    if not observed or observed[0] != expected[0] or any(r not in expected for r in observed):
                        raise ValueError("Review request history differs from its declared requests")
                    if expected[1] in observed and any(r != expected[1] for r in observed[observed.index(expected[1]):]):
                        raise ValueError("Review request history reverts after repair")
                elif observed != expected:
                    raise ValueError("Successful package request history must equal the full ordered probe")
                if not (len(row["findings"]) == len(row["verifications"]) == len(row["report_statuses"])):
                    raise ValueError("Verification outcomes do not cover every finding")
                remote_results = sum(v is not None and v.get("cache_status") not in ("local_skip", "hit")
                                     for v in row["verifications"])
                if sum(r["operation"] == "verification" for r in row["requests"]) < remote_results:
                    raise ValueError("Remote verification outcomes lack captured requests")
                for f, v, status in zip(row["findings"], row["verifications"], row["report_statuses"], strict=True):
                    finding = Finding(**{k: v for k, v in f.items() if k != "verification"})
                    finding.verification = VerificationResult(**v)
                    if v["verification_failed"] or status != classify_status(finding).value:
                        raise ValueError("Invalid successful verification status")
                    if status in ("NOT_CHECKED", "VERIFICATION_FAILED"):
                        raise ValueError("Successful record contains unchecked findings")
                for field in ("review_latency_seconds", "verification_latency_seconds"):
                    value = row[field]
                    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                        raise ValueError("Invalid measured phase latency")
                if sum(row[k] for k in ("review_latency_seconds", "verification_latency_seconds")) > latency + 0.01:
                    raise ValueError("Phase times exceed measured pipeline latency")
                cost = me.price_attempts(row["attempts"])
                if cost["unknown_usage"] or cost["unpriced"]:
                    problems.append(f"Unknown/unpriced cost {package.record_key(row)}")
                rows.append(row)
    return rows, problems


def adjudication_template(cases, rows):
    by_case = {c.case_id: c for c in cases}
    result = {}
    for row in rows:
        if row["status"] != "ok":
            continue
        provisional = package.classify(by_case[row["case_id"]], row)
        retained = {i for i, status in enumerate(row["report_statuses"]) if status in RETAINED_STATUSES}
        result[package.record_key(row)] = {"record_sha256": row["record_sha256"],
            "findings_sha256": digest(row["findings"]), "reviewed": False,
            "matches": provisional["matches"], "dispositions": {**provisional["dispositions"],
                **{i: None for i in provisional["unclassified"]}},
            "final_matches": {d.label: None for d in by_case[row["case_id"]].defects},
            "final_dispositions": {str(i): None for i in sorted(retained)}}
    return result


def classifications(case, row, judgment):
    if judgment.get("record_sha256") != row["record_sha256"]:
        raise ValueError("Stale adjudication: full record digest differs")
    if type(judgment.get("reviewed")) is not bool:
        raise ValueError("Adjudication reviewed flag must be boolean")
    raw = package.classify(case, row, judgment)
    if not raw["reviewed"]:
        return None
    retained = {i for i, status in enumerate(row["report_statuses"]) if status in RETAINED_STATUSES}
    matches = judgment["final_matches"]
    if set(matches) != {d.label for d in case.defects}:
        raise ValueError("Final judgments must address every expected defect")
    used = [i for i in matches.values() if i is not None]
    if any(type(i) is not int or i not in retained for i in used) or len(set(used)) != len(used):
        raise ValueError("Final matched indexes must be distinct retained findings")
    dispositions = judgment["final_dispositions"]
    if (set(dispositions) != {str(i) for i in retained - set(used)}
        or any(v not in ("supported", "unsupported", "duplicate") for v in dispositions.values())):
        raise ValueError("Classify every unmatched retained finding")
    # Fixed unsafe-edit traps cannot be made safe by a model verdict or judge.
    forbidden = {i for trap in case.traps for i, f in enumerate(row["findings"])
                 if str(f.get("actionType", "")).upper() in trap.actions and me._matches(trap.match_any, f)}
    if set(used) & forbidden or any(dispositions.get(str(i)) != "unsupported" for i in retained & forbidden):
        raise ValueError("Retained findings violating fixed traps must remain unsupported")
    return raw, {"matches": matches, "dispositions": dispositions, "finding_count": len(retained)}


def summarize(cases, rows, classified, *, final):
    by_case = {c.case_id: c for c in cases}
    recovered = severe_recovered = expected = severe_expected = clean_false_positives = unsupported_severe = 0
    counts = {"supported": 0, "unsupported": 0, "duplicate": 0}
    matched = {}
    for row in rows:
        case = by_case[row["case_id"]]
        result = classified[package.record_key(row)][int(final)]
        found = {label for label, index in result["matches"].items() if index is not None}
        matched[package.record_key(row)] = found
        expected += len(case.defects)
        severe_expected += sum(d.severe for d in case.defects)
        recovered += len(found)
        severe_recovered += sum(d.severe and d.label in found for d in case.defects)
        counts["supported"] += len(found)
        for index, disposition in result["dispositions"].items():
            counts[disposition] += 1
            unsupported_severe += (disposition == "unsupported" and
                row["findings"][int(index)]["severity"].upper() in review_ds.SEVERE_SEVERITIES)
        clean_false_positives += case.is_clean and "unsupported" in result["dispositions"].values()
    count = sum(counts.values())
    return {"recovered": recovered, "expected": expected, "recall": recovered / expected if expected else None,
        "severe_recovered": severe_recovered, "severe_expected": severe_expected,
        "severe_recall": severe_recovered / severe_expected if severe_expected else None,
        "precision": counts["supported"] / count if count else None, "finding_count": count,
        "duplicate_rate": counts["duplicate"] / count if count else 0.0,
        "clean_false_positives": clean_false_positives, "unsupported_severe": unsupported_severe,
        **counts}, matched


def decision(baseline, candidate, *, severe_losses, sufficient):
    """Fixture-only adopt/retain/reject; incomplete evidence always defers."""
    if not sufficient:
        return {"decision": "defer", "reasons": ["Insufficient unique cases, clean cases, severe defects or repetitions"]}
    reasons = []
    for view in ("raw", "final"):
        b, c = baseline[view], candidate[view]
        if c["severe_recall"] is None:
            return {"decision": "defer", "reasons": ["Severe recall is undefined"]}
        if c["recall"] < b["recall"]:
            reasons.append(f"{view}: recall regressed")
        if c["severe_recall"] < GATES["minimum_severe_recall"]:
            reasons.append(f"{view}: severe recall below floor")
        if c["precision"] is not None and (c["precision"] < GATES["minimum_precision"]
                or b["precision"] is not None and c["precision"] < b["precision"] - GATES["maximum_precision_drop"]):
            reasons.append(f"{view}: precision gate failed")
        if c["clean_false_positives"] > b["clean_false_positives"]:
            reasons.append(f"{view}: more clean-case false positives")
        if c["duplicate_rate"] > b["duplicate_rate"]:
            reasons.append(f"{view}: duplicate rate increased")
    if severe_losses:
        reasons.append("A baseline-recovered severe defect was lost in a paired case")
    if candidate["final"]["unsupported_severe"]:
        reasons.append("Retained unsupported CRITICAL/HIGH finding")
    if candidate["coverage_errors"] > GATES["maximum_coverage_errors"]:
        reasons.append("Package requirement coverage is incorrect or incomplete")
    cost_ratio = me._ratio(candidate["cost"]["usd"], baseline["cost"]["usd"])
    latency_ratio = me._ratio(candidate["p95_latency_seconds"], baseline["p95_latency_seconds"])
    if cost_ratio is None or latency_ratio is None:
        return {"decision": "defer", "reasons": ["Total cost or p95 latency comparison is undefined"]}
    if cost_ratio > GATES["maximum_cost_ratio"]:
        reasons.append("Total review-plus-verification cost exceeded allowance")
    if latency_ratio > GATES["maximum_p95_latency_ratio"]:
        reasons.append("Measured serial p95 latency exceeded allowance")
    if reasons:
        return {"decision": "reject", "reasons": reasons}
    quality_gain = candidate["final"]["recovered"] > baseline["final"]["recovered"]
    saving = cost_ratio <= GATES["cost_saving_ratio"]
    return {"decision": "adopt" if quality_gain or saving else "retain",
            "reasons": ["Verified/local retained recall improved" if quality_gain else
                        "At least 10% total cost reduction" if saving else "No qualifying quality or cost benefit"]}


def score(m, cases, rows, judgments=None, *, problems=()):
    judgments = judgments or {}
    classified, incomplete = {}, list(problems)
    expected_keys = {f"{a.arm_id}/{rep}/{c.case_id}" for a in arms(m["experiment"])
                     for rep in range(1, m["repetitions"] + 1) for c in cases}
    actual_keys = [package.record_key(r) for r in rows]
    if set(actual_keys) != expected_keys or len(actual_keys) != len(expected_keys):
        incomplete.append("Missing, duplicate or extra paired records")
    keys = {package.record_key(r) for r in rows if r["status"] == "ok"}
    if set(judgments) - keys:
        raise ValueError("Unknown or unsuccessful adjudication record")
    for row in rows:
        if row["status"] != "ok":
            incomplete.append(f"Unsuccessful {package.record_key(row)}")
            continue
        key = package.record_key(row)
        cost = me.price_attempts(row["attempts"])
        if cost["unknown_usage"] or cost["unpriced"]:
            incomplete.append(f"Unknown/unpriced cost {key}")
        case = next(c for c in cases if c.case_id == row["case_id"])
        if key not in judgments or not (result := classifications(case, row, judgments[key])):
            incomplete.append(f"Unadjudicated {key}")
        else:
            classified[key] = result
    baseline_arm, candidate_arm = arms(m["experiment"])
    output = {"manifest_sha256": digest(m), "cost_scope": COST_SCOPE, "stages": {},
        "production_decision": {"decision": "defer", "reasons": [
            "Constructed fixture evidence requires representative real-spec review before changing production defaults"]}}
    output["collection"] = {}
    for arm in (baseline_arm, candidate_arm):
        selected = [r for r in rows if r["arm_id"] == arm.arm_id]
        cost = me.price_attempts(a for r in selected for a in r.get("attempts", []))
        output["collection"][arm.arm_id] = {"cost": cost,
            "successful": sum(r["status"] == "ok" for r in selected),
            "failed": sum(r["status"] == "failed" for r in selected),
            "not_run": sum(r["status"] == "not_run" for r in selected),
            "cost_complete": (len(selected) == m["repetitions"] * len(cases)
                and all(r["status"] == "ok" for r in selected) and not cost["unknown_usage"] and not cost["unpriced"])}
    if incomplete or len(rows) != 2 * m["repetitions"] * len(cases):
        output["fixture_decision"] = {"decision": "defer", "reasons": sorted(set(incomplete)) or ["Missing paired records"]}
        output["quality"] = "withheld until all pairs, attempt costs and judgments are complete"
        return output
    for stage in sorted({c.stage for c in cases}):
        stage_cases = [c for c in cases if c.stage == stage]
        minimum = MINIMUMS[stage]
        sufficient = (m["split"] == "held_out" and m["repetitions"] >= GATES["minimum_repetitions"]
            and len(stage_cases) >= minimum["cases"] and sum(c.is_clean for c in stage_cases) >= minimum["clean"]
            and sum(d.severe for c in stage_cases for d in c.defects) >= minimum["severe"])
        summaries, matches = {}, {}
        for arm in (baseline_arm, candidate_arm):
            selected = [r for r in rows if r["arm_id"] == arm.arm_id and r["stage"] == stage]
            views = {}
            for final in (False, True):
                name = "final" if final else "raw"
                views[name], matches[arm.arm_id, name] = summarize(cases, selected, classified, final=final)
            views.update(cost=me.price_attempts(a for r in selected for a in r["attempts"]),
                cost_by_operation={op: me.price_attempts(a for r in selected for a in r["attempts"] if a["operation"] == op)
                    for op in (stage, "triage", "verification")},
                p50_latency_seconds=me.percentile([r["latency_seconds"] for r in selected], 50),
                p95_latency_seconds=me.percentile([r["latency_seconds"] for r in selected], 95),
                report_status_counts={status: sum(r["report_statuses"].count(status) for r in selected)
                    for status in sorted({s for r in selected for s in r["report_statuses"]})})
            coverage_errors = 0
            for row in selected:
                case = next(c for c in stage_cases if c.case_id == row["case_id"])
                expected_coverage = getattr(case, "expected_coverage", {})
                coverage = row["details"].get("coverage", [])
                for requirement_id, status in expected_coverage.items():
                    entries = [e for e in coverage if e.get("requirement_id") == requirement_id]
                    coverage_errors += not (len(entries) == 1 and entries[0].get("status") == status)
                coverage_errors += sum(e.get("requirement_id") not in expected_coverage for e in coverage)
            views["coverage_errors"] = coverage_errors
            summaries[arm.arm_id] = views
        severe_losses = []
        for rep in range(1, m["repetitions"] + 1):
            for case in stage_cases:
                for view in ("raw", "final"):
                    left = matches[baseline_arm.arm_id, view][f"{baseline_arm.arm_id}/{rep}/{case.case_id}"]
                    right = matches[candidate_arm.arm_id, view][f"{candidate_arm.arm_id}/{rep}/{case.case_id}"]
                    severe_losses.extend({"case_id": case.case_id, "repetition": rep, "view": view, "defect": d.label}
                        for d in case.defects if d.severe and d.label in left - right)
        output["stages"][stage] = {"arms": summaries, "severe_pair_losses": severe_losses,
            "fixture_decision": decision(summaries[baseline_arm.arm_id], summaries[candidate_arm.arm_id],
                                          severe_losses=severe_losses, sufficient=sufficient)}
    decisions = {s["fixture_decision"]["decision"] for s in output["stages"].values()}
    overall = next(d for d in ("defer", "reject", "adopt", "retain") if d in decisions)
    output["fixture_decision"] = {"decision": overall, "reasons": ["Each stage must pass its own declared gates"]}
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    declare_cli = sub.add_parser("declare", help="freeze gates and probes before any paid outputs (offline)")
    declare_cli.add_argument("--experiment", choices=EXPERIMENTS, required=True)
    declare_cli.add_argument("--split", choices=review_ds.SPLITS, default="held_out")
    declare_cli.add_argument("--repetitions", type=int, default=3)
    declare_cli.add_argument("--out", type=Path, required=True)
    probe_cli = sub.add_parser("_probe")
    probe_cli.add_argument("--experiment", choices=EXPERIMENTS, required=True)
    probe_cli.add_argument("--arm", required=True)
    probe_cli.add_argument("--split", choices=review_ds.SPLITS, required=True)
    for command in ("run", "_run-arm", "score", "adjudication-template"):
        p = sub.add_parser(command)
        p.add_argument("--out", type=Path, required=True)
        if command in ("run", "_run-arm"):
            p.add_argument("--cap-usd", type=float, required=True)
            p.add_argument("--live", action="store_true")
        if command == "_run-arm":
            p.add_argument("--arm", required=True)
            p.add_argument("--repetition", type=int, required=True)
        if command == "score":
            p.add_argument("--adjudication", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "declare":
            output = declare(args.out, experiment=args.experiment, split=args.split, repetitions=args.repetitions)
        elif args.command == "_probe":
            output = arm_probe(args.experiment, args.arm, args.split)
        elif args.command == "run":
            output = run_experiment(args.out, cap=args.cap_usd, live=args.live)
        elif args.command == "_run-arm":
            output = run_arm(args.out, arm_id=args.arm, repetition=args.repetition, cap=args.cap_usd, live=args.live)
        else:
            m, cases = load_manifest(args.out)
            rows, problems = load_records(args.out, m, cases)
            if args.command == "adjudication-template":
                output = adjudication_template(cases, rows)
            else:
                judgments = json.loads(args.adjudication.read_text(encoding="utf-8")) if args.adjudication else None
                output = score(m, cases, rows, judgments, problems=problems)
    except (ValueError, OSError, KeyError, TypeError, me.RunRefused, me.ArmStateError,
            subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(output, indent=2, ensure_ascii=False, default=package._jsonable))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
