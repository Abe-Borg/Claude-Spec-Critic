"""Isolated package-pass coverage experiment; offline unless ``run --live``.

Reuses model_effort's state isolation, matching and pricing, and production
request builders, streaming passes, chunk merger and coverage reconciliation.
Verification is deliberately not run: package cost is not total pipeline cost.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, is_dataclass
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Sequence
from unittest.mock import patch

from evals import model_effort as me
from evals import package_review_dataset as ds


ARMS = {name: me.Arm(name, None, rationale) for name, rationale in (
    ("baseline", "Merged prompt without the added confidence/severity coverage instructions."),
    ("coverage", "Shipped prompt, including the chunk-absence review fix."),
)}

# Frozen fragments fail closed if production wording changes. Both arms retain
# all grounding, zero-findings, hedging and whole-package/subset safeguards.
_CROSS_COVERAGE = (
    "Confidence labels the strength of the evidence for the downstream filter; "
    "it is not a gate on whether to report. Report every coordination issue you "
    "can ground in quoted spec text, including the ones you are uncertain about "
    "or consider low-severity — do not filter for importance or confidence at "
    "this stage. A separate verification pass filters and ranks findings; a "
    "real finding filtered out later is a normal outcome, while one withheld "
    "here is silently lost.\n"
)
_COMPLIANCE_COVERAGE = (
    "Confidence labels the strength of the evidence for the downstream filter;\n"
    "it is not a gate on whether to report. Report every missing or contradicted\n"
    "controlling requirement you can ground in the supplied profile and corpus,\n"
    "including the ones you are uncertain about or consider low-severity — do\n"
    "not filter for importance or confidence at this stage. "
)
_COMPLIANCE_FILTER = (
    " A separate verification pass filters and ranks findings; a real\n"
    "finding filtered out later is a normal outcome, while one withheld here\n"
    "is silently lost."
)


@contextmanager
def prompt_arm(arm_id: str):
    from src.cross_check import cross_checker as cross
    from src.compliance import compliance_checker as compliance

    if arm_id not in ARMS:
        raise ValueError(f"unknown arm {arm_id}")
    with ExitStack() as stack:
        for module, name, fragments in (
            (cross, "_cross_system_prompt", (_CROSS_COVERAGE,)),
            (compliance, "_compliance_system_prompt", (_COMPLIANCE_COVERAGE, _COMPLIANCE_FILTER)),
        ):
            original = getattr(module, name)

            def variant(cycle, original=original, fragments=fragments):
                prompt = original(cycle)
                for fragment in fragments:
                    if prompt.count(fragment) != 1:
                        raise me.RunRefused("Coverage fragment changed; revalidate the comparison before running.")
                    if arm_id == "baseline":
                        prompt = prompt.replace(fragment, "", 1)
                return prompt

            stack.enter_context(patch.object(module, name, variant))
        yield


def prepared_case(case: ds.PackageCase):
    from src.input.extractor import ExtractedSpec
    from src.research import RequirementsProfile, ResearchItem
    from src.review.reviewer import Finding

    specs = [ExtractedSpec(s.filename, s.text, len(s.text.split())) for s in case.specs]
    profile = RequirementsProfile(items=[ResearchItem(
        item_id=r.item_id, dimension_id="client_standards", topic="Constructed owner basis",
        category="client_standard", requirement=r.text, authority="Supplied fictional owner basis",
        grounded=r.grounded, confidence=1.0 if r.grounded else 0.3, actionability=r.actionability,
        accepted_sources=["https://owner.example.invalid/evaluation-basis"] if r.grounded else [],
        notes="Constructed fixture evidence; not an externally researched authority.",
    ) for r in case.requirements])
    return specs, profile, [Finding(**f) for f in case.existing_findings]


def partitions(case: ds.PackageCase) -> tuple[tuple[int, ...], ...]:
    return case.chunks or (tuple(range(len(case.specs))),)


def request_shape(params: dict) -> dict:
    return {"system_sha256": ds.digest(params.get("system")),
            "other_sha256": ds.digest({k: v for k, v in params.items() if k != "system"}),
            "model": params["model"]}


def request_probe(arm_id: str, cases: Sequence[ds.PackageCase]) -> dict:
    from src.cross_check.cross_checker import build_cross_check_request
    from src.compliance.compliance_checker import build_compliance_request
    from src.modules.registry import get_module
    from src.core.chunked_pass import filter_findings_for_chunk

    result = {}
    with prompt_arm(arm_id):
        for case in cases:
            specs, profile, existing = prepared_case(case)
            result[case.case_id] = []
            for indexes in partitions(case):
                subset = [specs[i] for i in indexes]
                scoped_findings = filter_findings_for_chunk(existing, {s.filename for s in subset}) if case.chunks else existing
                kwargs = dict(project_context=ds.PROJECT_CONTEXT, cycle=get_module(case.module_id).cycle,
                              chunk_subset=bool(case.chunks))
                if case.stage == "cross_check":
                    params = build_cross_check_request(subset, scoped_findings, **kwargs)
                else:
                    params = build_compliance_request(subset, profile, scoped_findings, **kwargs)
                result[case.case_id].append(request_shape(params))
    return result


def comparison_probe(cases: Sequence[ds.PackageCase]) -> dict:
    probes = {arm: request_probe(arm, cases) for arm in ARMS}
    for case in cases:
        for base, candidate in zip(probes["baseline"][case.case_id], probes["coverage"][case.case_id], strict=True):
            if base["other_sha256"] != candidate["other_sha256"] or base["system_sha256"] == candidate["system_sha256"]:
                raise me.RunRefused(f"{case.case_id}: comparison must change only system coverage wording")
    return probes


def source_digest() -> str:
    root = Path(__file__).resolve().parents[1]
    return ds.digest({str(p.relative_to(root)): p.read_text(encoding="utf-8")
                      for folder in ("src", "evals") for p in sorted((root / folder).rglob("*.py"))})


def _jsonable(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    return str(value)


class AttemptLedger:
    """Check between requests and record usage even if parsing later fails.

    A single in-flight request may exceed the cap. Unknown usage or an unpriced
    model stops further sends; it is never silently treated as zero spend.
    """
    def __init__(self, cap: float):
        if not math.isfinite(cap) or cap <= 0:
            raise me.RunRefused("A finite positive spending cap is required.")
        self.cap = cap
        self.attempts: list[dict] = []
        self.requests: list[dict] = []
        self.responses: list[Any] = []

    def stop_reason(self) -> str:
        cost = me.price_attempts(self.attempts)
        if cost["unknown_usage"] or cost["unpriced"]:
            return "Usage is unknown or unpriced; further requests refused."
        if cost["usd"] >= self.cap:
            return "Spending cap reached; further requests refused."
        return ""

    @contextmanager
    def stream(self, delegate, operation: str, **params):
        from src.core.attempt_usage import known_attempt, unknown_attempt

        reason = self.stop_reason()
        if reason:
            raise me.RunRefused(reason)
        self.requests.append(request_shape(params))
        recorded = False
        ledger = self

        class RecordedStream:
            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def get_final_message(self):
                nonlocal recorded
                message = self.inner.get_final_message()
                if not recorded:
                    usage = getattr(message, "usage", None)
                    kwargs = dict(operation=operation, transport="realtime", model=getattr(message, "model", None) or params["model"])
                    attempt = (known_attempt(usage, message_id=getattr(message, "id", ""), **kwargs)
                               if usage is not None else unknown_attempt(**kwargs))
                    ledger.attempts.append(attempt.to_dict())
                    ledger.responses.append(json.loads(json.dumps(getattr(message, "content", []), default=_jsonable)))
                    recorded = True
                return message

        try:
            with delegate.stream(**params) as inner:
                yield RecordedStream(inner)
        finally:
            if not recorded:
                self.attempts.append(unknown_attempt(operation=operation, transport="realtime",
                                                     model=params["model"], outcome="stream_failed").to_dict())
                self.responses.append(None)


class RecordedClient:
    def __init__(self, client, ledger: AttemptLedger, operation: str):
        self.client = client
        self.ledger = ledger
        self.operation = operation
        self.messages = self

    def stream(self, **params):
        return self.ledger.stream(self.client.messages, self.operation, **params)

    def __getattr__(self, name):
        return getattr(self.client.messages, name)


def execute_case(case: ds.PackageCase):
    from src.cross_check import cross_checker as cross
    from src.compliance import compliance_checker as compliance
    from src.core.chunked_pass import run_chunked_pass
    from src.modules.registry import get_module

    specs, profile, existing = prepared_case(case)
    module = get_module(case.module_id)

    def run(spec_subset, findings, *, chunk_subset):
        kwargs = dict(project_context=ds.PROJECT_CONTEXT, cycle=module.cycle,
                      max_retries=1, chunk_subset=chunk_subset)
        if case.stage == "cross_check":
            return cross.run_cross_check(spec_subset, findings, **kwargs)
        return compliance.run_compliance_check(spec_subset, profile, findings, **kwargs)

    if not case.chunks:
        return run(specs, existing, chunk_subset=False)
    # Exercise the production chunk merger with explicit fixture partitions.
    chunks = [(f"fixture-{n}", [specs[i] for i in indexes]) for n, indexes in enumerate(case.chunks)]
    from src.core.api_config import COMPLIANCE_MODEL_DEFAULT, CROSS_CHECK_MODEL_DEFAULT
    return run_chunked_pass(chunks, existing, groups=module.cross_check_chunk_groups,
        run_chunk=lambda job: run(job.specs, job.existing_findings, chunk_subset=True),
        pass_name=case.stage, summary_title="Package evaluation",
        model=CROSS_CHECK_MODEL_DEFAULT if case.stage == "cross_check" else COMPLIANCE_MODEL_DEFAULT,
        finalize=compliance.coverage_finalizer(profile) if case.stage == "compliance" else None)


def record_path(out: Path, arm_id: str, repetition: int) -> Path:
    return out / f"{arm_id}.package.r{repetition}.jsonl"


def run_arm(arm_id: str, out: Path, *, split: str, repetition: int, cap: float, live: bool) -> dict:
    from src.cross_check import cross_checker as cross
    from src.compliance import compliance_checker as compliance

    me.check_run_preconditions(ARMS[arm_id], out_dir=out, stage="package", repetition=repetition,
                              live=live, max_spend_usd=cap)
    cases = tuple(c for c in ds.load_dataset() if c.split == split)
    if ds.validate_dataset():
        raise me.RunRefused("Invalid package dataset")
    probe = request_probe(arm_id, cases)
    source_sha = source_digest()
    manifest = json.loads((out / "experiment.json").read_text(encoding="utf-8"))
    if (manifest["source_sha256"] != source_sha or manifest["split"] != split
        or manifest["probes"][arm_id] != probe
        or manifest["dataset_sha256"] != ds.digest([asdict(c) for c in cases])):
        raise me.RunRefused("Experiment source, dataset or request shape changed before this arm started.")
    ledger = AttemptLedger(cap)
    out.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(prompt_arm(arm_id))
        for module, operation in ((cross, "cross_check"), (compliance, "compliance")):
            factory = module._get_client

            def client_factory(*args, factory=factory, operation=operation, **kwargs):
                client = factory(*args, **kwargs)
                if hasattr(client, "close"):
                    stack.callback(client.close)
                return RecordedClient(client, ledger, operation)

            stack.enter_context(patch.object(module, "_get_client", client_factory))
        with record_path(out, arm_id, repetition).open("x", encoding="utf-8") as fh:
            for case in cases:
                base = dict(arm_id=arm_id, case_id=case.case_id, split=split, repetition=repetition,
                            case_sha256=ds.case_digest(case), source_sha256=source_sha,
                            request_probe=probe[case.case_id], verification="not_run", total_pipeline_cost_usd=None)
                if ledger.stop_reason():
                    fh.write(json.dumps({**base, "status": "not_run", "reason": ledger.stop_reason()}) + "\n")
                    fh.flush()
                    continue
                start = time.monotonic()
                offset = len(ledger.attempts)
                try:
                    result = execute_case(case)
                    findings = [{**me._finding_dict(f), "fileName": f.fileName,
                                 "affected_files": f.affected_files} for f in result.findings]
                    status = ("ok" if result.cross_check_status == "completed" and not result.error
                              and not result.chunk_failures and not result.chunk_skips else "failed")
                    outcome = dict(status=status, error=result.error, parse_status=result.parse_status,
                                   pass_status=result.cross_check_status, chunk_failures=result.chunk_failures,
                                   chunk_skips=result.chunk_skips, findings=findings, coverage=result.coverage,
                                   raw_response=result.raw_response, summary=result.thinking)
                except Exception as exc:
                    outcome = dict(status="failed", error=f"{type(exc).__name__}: {exc}", findings=[], coverage=[])
                attempts = ledger.attempts[offset:]
                record = {**base, **outcome, "attempts": attempts, "cost": me.price_attempts(attempts),
                          "requests": ledger.requests[offset:], "responses": ledger.responses[offset:],
                          "latency_seconds": round(time.monotonic() - start, 3)}
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
    return {"arm_id": arm_id, "repetition": repetition, "cases": len(cases),
            "cost": me.price_attempts(ledger.attempts), "stopped_reason": ledger.stop_reason()}


def run_experiment(out: Path, *, split: str, repetitions: int, cap: float, live: bool) -> dict:
    if not live:
        raise me.RunRefused("Paid runs require --live, ANTHROPIC_API_KEY and a finite positive cap.")
    AttemptLedger(cap)
    if repetitions < 1:
        raise me.RunRefused("Repetitions must be positive.")
    if not os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") == me._SENTINEL_KEY:
        raise me.RunRefused("No real ANTHROPIC_API_KEY in the environment.")
    if out.exists() and any(out.iterdir()):
        raise me.RunRefused("Use an empty output directory; records and state must be fresh.")
    cases = tuple(c for c in ds.load_dataset() if c.split == split)
    if ds.validate_dataset() or not cases:
        raise me.RunRefused("Invalid/empty dataset selection.")
    # Probe in an isolated subprocess too: operator overrides must not leak into
    # the comparison manifest while the actual arms have those overrides removed.
    out.mkdir(parents=True, exist_ok=True)
    probe_env = me.arm_environment(ARMS["baseline"], state_dir=out.resolve() / "probe-state")
    completed = subprocess.run([sys.executable, "-m", "evals.package_review", "probe", "--split", split],
                               env=probe_env, capture_output=True, text=True, check=True)
    probes = json.loads(completed.stdout)
    manifest = dict(split=split, repetitions=repetitions, cap_usd=cap,
                    dataset_sha256=ds.digest([asdict(c) for c in cases]), source_sha256=source_digest(),
                    probes=probes, verification="not_run", total_pipeline_cost_usd=None,
                    cost_scope="package passes only; one in-flight request may overshoot the cap")
    (out / "experiment.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    summaries = []
    allocation = cap / (2 * repetitions)
    for repetition in range(1, repetitions + 1):
        order = ("baseline", "coverage") if repetition % 2 else ("coverage", "baseline")
        for arm_id in order:
            remaining = cap - sum(s["cost"]["usd"] for s in summaries)
            if remaining <= 0:
                raise me.RunRefused("Experiment spending cap reached; partial records retained.")
            state = me.prepare_state_dir(out / "state" / f"r{repetition}", ARMS[arm_id])
            env = me.arm_environment(ARMS[arm_id], state_dir=state)
            process = subprocess.run([sys.executable, "-m", "evals.package_review", "_run-arm",
                "--arm", arm_id, "--out", str(out.resolve()), "--split", split,
                "--repetition", str(repetition), "--cap-usd", str(min(allocation, remaining)), "--live"],
                env=env, capture_output=True, text=True)
            if process.returncode:
                raise me.RunRefused(f"{arm_id} r{repetition} failed: {process.stderr.strip()}")
            summary = json.loads(process.stdout)
            summaries.append(summary)
            (out / "runs.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
            if summary["cost"]["unknown_usage"] or summary["cost"]["unpriced"]:
                raise me.RunRefused("Usage unknown/unpriced: experiment stopped, partial records retained.")
    return {"out": str(out), "runs": summaries, "verification": "not_run"}


def load_experiment(out: Path):
    manifest = json.loads((out / "experiment.json").read_text(encoding="utf-8"))
    cases = tuple(c for c in ds.load_dataset() if c.split == manifest["split"])
    if manifest["dataset_sha256"] != ds.digest([asdict(c) for c in cases]):
        raise ValueError("Dataset changed; score with the dataset that produced the records.")
    by_case = {c.case_id: c for c in cases}
    records = []
    for repetition in range(1, manifest["repetitions"] + 1):
        for arm_id in ARMS:
            path = record_path(out, arm_id, repetition)
            if not path.exists():
                raise ValueError(f"Missing paired records: {path.name}")
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if len(rows) != len(cases) or {r["case_id"] for r in rows} != set(by_case):
                raise ValueError("Missing, duplicate or extra case records.")
            for row in rows:
                case = by_case[row["case_id"]]
                if (row["arm_id"] != arm_id or row["repetition"] != repetition or row["split"] != case.split
                    or row["case_sha256"] != ds.case_digest(case) or row["source_sha256"] != manifest["source_sha256"]
                    or row["request_probe"] != manifest["probes"][arm_id][case.case_id]):
                    raise ValueError("Records have incompatible arm, source, case or request fingerprints.")
                if row["status"] not in {"ok", "failed", "not_run"}:
                    raise ValueError("Invalid record status")
                allowed = row["request_probe"]
                if any(request not in allowed for request in row.get("requests", [])):
                    raise ValueError("An actual request differs from the controlled request probe.")
                records.append(row)
    return cases, records


def record_key(row: dict) -> str:
    return f"{row['arm_id']}/{row['repetition']}/{row['case_id']}"


def classify(case: ds.PackageCase, row: dict, judgment: dict | None = None) -> dict:
    findings = row.get("findings", [])
    provisional = me.match_review(case, findings)
    forbidden = {i for trap in case.traps for i, finding in enumerate(findings)
                 if str(finding.get("actionType", "")).upper() in trap.actions and me._matches(trap.match_any, finding)}
    reviewed = bool(judgment and judgment.get("reviewed"))
    if judgment is not None and judgment.get("findings_sha256") != ds.digest(findings):
        raise ValueError("Stale adjudication: findings digest differs.")
    matches = provisional["matched"]
    dispositions = {str(i): "unsupported" for i in forbidden}
    if reviewed:
        matches = judgment.get("matches", {})
        if set(matches) != {d.label for d in case.defects}:
            raise ValueError("Adjudication must address every expected defect.")
        used = [i for i in matches.values() if i is not None]
        if (any(type(i) is not int or i < 0 or i >= len(findings) for i in used)
            or len(set(used)) != len(used) or set(used) & forbidden):
            raise ValueError("Invalid, reused or forbidden matched finding index.")
        dispositions = judgment.get("dispositions", {})
        extras = {str(i) for i in range(len(findings)) if i not in used}
        if set(dispositions) != extras or any(v not in {"supported", "unsupported", "duplicate"} for v in dispositions.values()):
            raise ValueError("Classify every unmatched finding as supported, unsupported or duplicate.")
        if any(dispositions.get(str(i)) != "unsupported" for i in forbidden):
            raise ValueError("A finding that violates a fixed trap must remain unsupported.")
    used = {i for i in matches.values() if i is not None}
    duplicates = {str(i) for i, finding in enumerate(findings) if i not in used and i not in forbidden
                  and any(me._matches(d.match_any, finding) for d in case.defects if matches[d.label] is not None)}
    if not reviewed:
        dispositions.update({i: "duplicate" for i in duplicates})
    extras = {str(i) for i in range(len(findings)) if i not in used}
    return {"matches": matches, "reviewed": reviewed, "dispositions": dispositions,
            "unclassified": sorted(extras - set(dispositions)), "finding_count": len(findings)}


def adjudication_template(cases, records) -> dict:
    by_case = {c.case_id: c for c in cases}
    result = {}
    for row in records:
        if row["status"] != "ok":
            continue
        matched = classify(by_case[row["case_id"]], row)
        result[record_key(row)] = dict(findings_sha256=ds.digest(row.get("findings", [])), reviewed=False,
            matches=matched["matches"], dispositions={**matched["dispositions"], **{i: None for i in matched["unclassified"]}})
    return result


def score(cases, records, judgments: dict | None = None, *, _per_stage: bool = True) -> dict:
    by_case = {c.case_id: c for c in cases}
    judgments = judgments or {}
    keys = {record_key(row) for row in records if row["status"] == "ok"}
    if set(judgments) - keys:
        raise ValueError("Adjudication contains unknown or unsuccessful records.")
    summaries = {}
    classifications = {}
    for arm in ARMS:
        rows = [r for r in records if r["arm_id"] == arm]
        total = severe_total = recovered = severe_recovered = 0
        counts = dict(supported=0, unsupported=0, duplicate=0, unclassified=0)
        clean_with_unsupported = 0
        coverage_correct = coverage_total = unexpected_coverage = 0
        all_reviewed = True
        for row in rows:
            case = by_case[row["case_id"]]
            total += len(case.defects)
            severe_total += sum(d.severe for d in case.defects)
            if row["status"] != "ok":
                all_reviewed = False
                coverage_total += len(case.expected_coverage)
                continue
            result = classify(case, row, judgments.get(record_key(row)))
            classifications[record_key(row)] = result
            all_reviewed &= result["reviewed"]
            found = {label for label, index in result["matches"].items() if index is not None}
            recovered += len(found)
            severe_recovered += sum(d.severe and d.label in found for d in case.defects)
            counts["supported"] += len(found)
            for disposition in result["dispositions"].values():
                counts[disposition] += 1
            counts["unclassified"] += len(result["unclassified"])
            clean_with_unsupported += case.is_clean and "unsupported" in result["dispositions"].values()
            coverage = row.get("coverage", [])
            coverage_total += len(case.expected_coverage)
            for rid, status in case.expected_coverage.items():
                entries = [r for r in coverage if r.get("requirement_id") == rid]
                coverage_correct += len(entries) == 1 and entries[0].get("status") == status
            unexpected_coverage += sum(r.get("requirement_id") not in case.expected_coverage for r in coverage)
        cost = me.price_attempts(a for row in rows for a in row.get("attempts", []))
        finding_total = sum(counts.values())
        summaries[arm] = dict(records=len(rows), successful=sum(r["status"] == "ok" for r in rows),
            failed=sum(r["status"] == "failed" for r in rows), not_run=sum(r["status"] == "not_run" for r in rows),
            recovered=recovered, expected=total, severe_recovered=severe_recovered, severe_expected=severe_total,
            provisional_recall=recovered / total if total else None,
            provisional_severe_recall=severe_recovered / severe_total if severe_total else None,
            adjudicated_recall=recovered / total if total and all_reviewed else None,
            adjudicated_severe_recall=severe_recovered / severe_total if severe_total and all_reviewed else None,
            finding_precision=counts["supported"] / finding_total if finding_total and all_reviewed else None,
            all_successful_and_adjudicated=all_reviewed, finding_counts=counts,
            clean_packages_with_known_false_positives=clean_with_unsupported,
            clean_packages=sum(by_case[r["case_id"]].is_clean for r in rows),
            coverage_correct=coverage_correct, coverage_expected=coverage_total,
            unexpected_coverage_rows=unexpected_coverage, package_cost=cost,
            latency_p50_seconds=me.percentile([r["latency_seconds"] for r in rows if "latency_seconds" in r], 50),
            verification="not_run", total_pipeline_cost_usd=None)
    pairs = []
    lookup = {(r["arm_id"], r["repetition"], r["case_id"]): r for r in records}
    for row in records:
        if row["arm_id"] != "baseline":
            continue
        candidate = lookup.get(("coverage", row["repetition"], row["case_id"]))
        eligible = bool(candidate and row["status"] == candidate["status"] == "ok")
        pairs.append(dict(case_id=row["case_id"], repetition=row["repetition"], comparable=eligible,
            baseline=classifications.get(record_key(row)),
            coverage=classifications.get(record_key(candidate)) if candidate else None))
    output = {"arms": summaries, "pairs": pairs,
              "decision": "Measurement infrastructure only; no adoption decision or live-quality claim."}
    if _per_stage:
        output["by_stage"] = {}
        for stage in ("cross_check", "compliance"):
            stage_cases = tuple(c for c in cases if c.stage == stage)
            stage_ids = {c.case_id for c in stage_cases}
            stage_rows = [r for r in records if r["case_id"] in stage_ids]
            stage_keys = {record_key(r) for r in stage_rows}
            output["by_stage"][stage] = score(stage_cases, stage_rows,
                {k: v for k, v in judgments.items() if k in stage_keys}, _per_stage=False)["arms"]
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    probe = sub.add_parser("probe")
    probe.add_argument("--split", choices=("tuning", "held_out", "all"), default="all")
    for command in ("run", "_run-arm"):
        runner = sub.add_parser(command)
        runner.add_argument("--out", type=Path, required=True)
        runner.add_argument("--split", choices=("tuning", "held_out"), default="held_out")
        runner.add_argument("--cap-usd", type=float, required=True)
        runner.add_argument("--live", action="store_true")
        if command == "run":
            runner.add_argument("--repetitions", type=int, default=3)
        else:
            runner.add_argument("--arm", choices=tuple(ARMS), required=True)
            runner.add_argument("--repetition", type=int, required=True)
    for command in ("score", "adjudication-template"):
        scorer = sub.add_parser(command)
        scorer.add_argument("--out", type=Path, required=True)
        if command == "score":
            scorer.add_argument("--adjudication", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            problems = ds.validate_dataset()
            output = {"cases": len(ds.load_dataset()), "problems": problems, "evidence": "constructed owner basis"}
            if problems:
                print(json.dumps(output, indent=2))
                return 1
        elif args.command == "probe":
            cases = tuple(c for c in ds.load_dataset() if args.split == "all" or c.split == args.split)
            output = comparison_probe(cases)
        elif args.command == "run":
            output = run_experiment(args.out, split=args.split, repetitions=args.repetitions, cap=args.cap_usd, live=args.live)
        elif args.command == "_run-arm":
            output = run_arm(args.arm, args.out, split=args.split, repetition=args.repetition, cap=args.cap_usd, live=args.live)
        else:
            cases, records = load_experiment(args.out)
            judgments = json.loads(args.adjudication.read_text(encoding="utf-8")) if args.command == "score" and args.adjudication else None
            output = score(cases, records, judgments) if args.command == "score" else adjudication_template(cases, records)
    except (ValueError, OSError, me.RunRefused, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
