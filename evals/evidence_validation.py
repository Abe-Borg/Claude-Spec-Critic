"""EX-04: evidence validation (observation mode) and source reuse.

The decision record is ``plans/experiments/EX-04-evidence-validation-source-reuse.md``.
Two changes, decided separately:

* **A. Evidence validation** — :mod:`src.verification.evidence_validation`,
  switched on with ``SPEC_CRITIC_EVIDENCE_VALIDATION=observe``. It records
  whether each conclusive verdict's quoted evidence agrees with it and never
  acts on the answer. This module scores it against the constructed evidence
  set (:mod:`evals.evidence_validation_dataset`) and turns a real run's
  diagnostics export into the table of disagreements a person adjudicates.
* **B. Source reuse** — :mod:`src.verification.source_reuse`, switched on with
  ``SPEC_CRITIC_SOURCE_REUSE=shadow`` (record only, either transport) or
  ``supply`` (real-time only). This module reads its rollup out of a
  diagnostics export and holds the protocol for the paired comparison.

Offline, nothing here calls a model. Usage::

    python -m evals.evidence_validation score [--split tuning|held_out|all]
    python -m evals.evidence_validation disagreements EXPORT.json [--markdown]
    python -m evals.evidence_validation reuse EXPORT.json
    python -m evals.evidence_validation protocol

``EXPORT.json`` is a diagnostics summary: the Diagnostics window's Save as
JSON, ``scripts/recover_batch.py --diagnostics-json``, or any JSON object with
the summary under ``"summary"``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from evals import evidence_validation_dataset as ds
from evals.model_effort import wilson_interval
from src.verification import evidence_validation as ev
from src.verification import source_reuse as sr

# ---------------------------------------------------------------------------
# Scoring the constructed set
# ---------------------------------------------------------------------------

_PREDICTION = {
    True: ds.SUPPORT_SUPPORTS,
    False: ds.SUPPORT_DOES_NOT,
    None: ds.SUPPORT_CANNOT_TELL,
}


def predict(case: ds.EvidenceCase) -> dict[str, Any]:
    """Run the validator on one case; return its prediction and the check gaps."""
    assessment = ev.assess_evidence(
        SimpleNamespace(**case.finding), SimpleNamespace(**case.verification)
    )
    if assessment["assessment"] == ev.ASSESSMENT_NOT_APPLICABLE:
        predicted = ds.SUPPORT_NOT_APPLICABLE
    else:
        predicted = _PREDICTION[assessment.get("agrees_with_verdict")]
    checks = assessment.get("checks") or {}
    check_misses = {
        name: {"expected": expected, "got": (checks.get(name) or {}).get("status")}
        for name, expected in case.expected_checks.items()
        if (checks.get(name) or {}).get("status") != expected
    }
    return {
        "case_id": case.case_id,
        "split": case.split,
        "category": case.category,
        "label": case.support,
        "predicted": predicted,
        "correct": predicted == case.support,
        "concerns": list(assessment.get("concerns") or []),
        "check_misses": check_misses,
        "lexical_overlap": (assessment.get("features") or {}).get("lexical_overlap"),
    }


def _rate(count: int, n: int) -> dict[str, Any]:
    return {
        "count": count,
        "n": n,
        "rate": round(count / n, 4) if n else None,
        "wilson95": wilson_interval(count, n),
    }


def score(outcomes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Agreement with the adjudicated labels, and the error each way.

    ``flag_precision`` — of the verdicts the validator flags, how many truly
    lack support; ``flag_recall`` — of the verdicts that truly lack support,
    how many it flags; ``false_concern_rate`` — of the verdicts the evidence
    does support, how many it flags (the cost of an enforcing rule: correct
    verdicts discarded). Cases labelled ``not_applicable`` are excluded from
    every rate.
    """
    applicable = [o for o in outcomes if o["label"] != ds.SUPPORT_NOT_APPLICABLE]
    flagged = [o for o in applicable if o["predicted"] == ds.SUPPORT_DOES_NOT]
    unsupported = [o for o in applicable if o["label"] == ds.SUPPORT_DOES_NOT]
    supported = [o for o in applicable if o["label"] == ds.SUPPORT_SUPPORTS]
    by_category: dict[str, dict[str, int]] = {}
    for o in outcomes:
        bucket = by_category.setdefault(o["category"], {"n": 0, "correct": 0})
        bucket["n"] += 1
        bucket["correct"] += int(bool(o["correct"]))
    check_total = sum(1 for o in outcomes for _ in ds_expected(o))
    check_misses = sum(len(o["check_misses"]) for o in outcomes)
    return {
        "cases": len(outcomes),
        "applicable": len(applicable),
        "label_agreement": _rate(sum(1 for o in applicable if o["correct"]), len(applicable)),
        "flag_precision": _rate(sum(1 for o in flagged if o["label"] == ds.SUPPORT_DOES_NOT), len(flagged)),
        "flag_recall": _rate(sum(1 for o in unsupported if o["predicted"] == ds.SUPPORT_DOES_NOT), len(unsupported)),
        "false_concern_rate": _rate(sum(1 for o in supported if o["predicted"] == ds.SUPPORT_DOES_NOT), len(supported)),
        "supported_confirmed": _rate(sum(1 for o in supported if o["predicted"] == ds.SUPPORT_SUPPORTS), len(supported)),
        "check_expectations": {"n": check_total, "misses": check_misses},
        "by_category": by_category,
        "misses": [
            {k: o[k] for k in ("case_id", "category", "label", "predicted", "concerns", "check_misses")}
            for o in outcomes
            if not o["correct"] or o["check_misses"]
        ],
    }


def ds_expected(outcome: Mapping[str, Any]) -> list[str]:
    case = _CASES_BY_ID[outcome["case_id"]]
    return list(case.expected_checks)


_CASES_BY_ID = {case.case_id: case for case in ds.CASES}


def score_split(split: str = ds.SPLIT_HELD_OUT) -> dict[str, Any]:
    cases = ds.CASES if split == "all" else ds.split_cases(split)
    report = score([predict(case) for case in cases])
    report["split"] = split
    report["dataset_version"] = ds.DATASET_VERSION
    report["dataset_digest"] = ds.dataset_digest()
    report["policy_version"] = ev.POLICY_VERSION
    return report


#: The held-out split, scored once with the ``ev1`` rules frozen (2026-09-29).
#: Tests reproduce it exactly: a change to the rules that moves any of these
#: numbers is a new policy version, and needs a new held-out set to be judged
#: on — never a quiet re-score of this one.
RECORDED_HELD_OUT_RESULT: dict[str, Any] = {
    "policy_version": "ev1",
    "dataset_digest": "f4aaa311bca9ce74228200aa5ef6a1093e860b5a13f61cec71c649f672af6d6c",
    "applicable": 17,
    "label_agreement": (12, 17),
    "flag_precision": (5, 7),
    "flag_recall": (5, 7),
    "false_concern_rate": (0, 7),
    "missed_case_ids": ("ev-h08", "ev-h10", "ev-h11", "ev-h13", "ev-h15"),
}

# ---------------------------------------------------------------------------
# Reading a real run
# ---------------------------------------------------------------------------


def load_summary(path: str | Path) -> dict[str, Any]:
    """A diagnostics summary from a JSON file (bare, or under ``"summary"``)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, Mapping) and isinstance(data.get("summary"), Mapping):
        return dict(data["summary"])
    if not isinstance(data, Mapping):
        raise ValueError(f"{path}: not a JSON object")
    return dict(data)


def disagreements(summary: Mapping[str, Any]) -> dict[str, Any]:
    """The run's observation-mode disagreements, for a person to adjudicate.

    Each row is a verdict the validator says its evidence does not support,
    with the reason for every concern. The ``adjudication`` column is empty on
    purpose: the protocol asks a person to fill it (``validator right`` /
    ``verifier right`` / ``both wrong`` / ``cannot tell``) before any rate is
    computed.
    """
    rollup = summary.get("evidence_validation")
    if not isinstance(rollup, Mapping):
        return {
            "recorded": False,
            "reason": "the summary has no evidence_validation rollup (run with "
            "SPEC_CRITIC_EVIDENCE_VALIDATION=observe)",
        }
    rows = [
        {
            **row,
            "adjudication": "",
        }
        for row in rollup.get("disagreement_findings") or []
    ]
    return {
        "recorded": True,
        "policy_version": rollup.get("policy_version"),
        "assessed": rollup.get("assessed", 0),
        "disagreements": rollup.get("disagreements", 0),
        "not_listed": rollup.get("disagreements_not_listed", 0),
        "by_concern": dict(rollup.get("by_concern") or {}),
        "by_verdict": dict(rollup.get("by_verdict") or {}),
        "by_provenance": dict(rollup.get("by_provenance") or {}),
        "rows": rows,
    }


def disagreements_markdown(table: Mapping[str, Any]) -> str:
    if not table.get("recorded"):
        return f"No evidence-validation record: {table.get('reason', '')}\n"
    lines = [
        f"Evidence validation ({table.get('policy_version')}): {table['disagreements']} of "
        f"{table['assessed']} assessed verdicts disagree with the verifier.",
        "",
        "| Finding | File | Verdict | Concerns | Why | Adjudication |",
        "|---|---|---|---|---|---|",
    ]
    for row in table["rows"]:
        why = "; ".join(f"{k}: {v}" for k, v in (row.get("concern_details") or {}).items())
        lines.append(
            "| {fid} | {file} | {verdict} | {concerns} | {why} | |".format(
                fid=row.get("finding_id", ""),
                file=str(row.get("file", "")).replace("|", "\\|"),
                verdict=row.get("verdict", ""),
                concerns=", ".join(row.get("concerns") or []),
                why=why.replace("|", "\\|"),
            )
        )
    if table.get("not_listed"):
        lines.append("")
        lines.append(f"{table['not_listed']} more disagreement(s) not listed (the rollup keeps the first {ev.MAX_LISTED_DISAGREEMENTS}).")
    return "\n".join(lines) + "\n"


#: The diagnostics phase both drivers log second-round verifications under
#: (cross-check and compliance findings). First-round lookups can never match:
#: the store is empty until the first round ends.
SECOND_ROUND_PHASE = "cross_check_verification"
#: Stage 1's gate (:data:`REUSE_PROTOCOL`), in numbers.
REUSE_GATE_MIN_RATE = 0.15
REUSE_GATE_MIN_LOOKUPS = 200


def _per_match(passages: int, matched: int) -> float | None:
    return round(passages / matched, 2) if matched else None


def reuse_measurement(summary: Mapping[str, Any]) -> dict[str, Any]:
    """What a run's source-reuse rollup says, and what it cannot say.

    Stage 1 of :data:`REUSE_PROTOCOL` reads two numbers, both bounds on what
    supplying could ever save: the **second-round match rate** (how many
    second-round verifications found passages the first round retrieved under
    the same claim context — first-round lookups are excluded, since they
    cannot match) and the **passage yield** (passages per match). The gate is
    evaluated on the second round alone. The search counts are telemetry; only
    the paired comparison turns them into a saving.
    """
    rollup = summary.get("source_reuse")
    if not isinstance(rollup, Mapping):
        return {
            "recorded": False,
            "reason": "the summary has no source_reuse rollup (run with "
            "SPEC_CRITIC_SOURCE_REUSE=shadow or supply)",
        }
    lookups = int(rollup.get("lookups", 0) or 0)
    matched = int(rollup.get("findings_matched_in_shadow", 0) or 0) + int(
        rollup.get("findings_supplied", 0) or 0
    )
    passages_matched = int(rollup.get("passages_matched_in_shadow", 0) or 0) + int(
        rollup.get("passages_supplied", 0) or 0
    )
    by_status = dict(rollup.get("by_status") or {})
    by_phase = rollup.get("by_phase") if isinstance(rollup.get("by_phase"), Mapping) else {}
    second = by_phase.get(SECOND_ROUND_PHASE) if isinstance(by_phase, Mapping) else None
    if isinstance(second, Mapping) and int(second.get("lookups", 0) or 0):
        second_lookups = int(second.get("lookups", 0) or 0)
        second_matched = int(second.get("matched", 0) or 0)
        second_round = {
            "recorded": True,
            "lookups": second_lookups,
            "by_status": dict(second.get("by_status") or {}),
            "match_rate": _rate(second_matched, second_lookups),
            "passages_matched": int(second.get("passages_matched", 0) or 0),
            "passages_per_match": _per_match(int(second.get("passages_matched", 0) or 0), second_matched),
        }
        rate = second_matched / second_lookups
        gate = {
            "min_rate": REUSE_GATE_MIN_RATE,
            "min_lookups": REUSE_GATE_MIN_LOOKUPS,
            "passes": (rate >= REUSE_GATE_MIN_RATE if second_lookups >= REUSE_GATE_MIN_LOOKUPS else None),
            "note": "" if second_lookups >= REUSE_GATE_MIN_LOOKUPS
            else f"too few second-round lookups to decide ({second_lookups} of {REUSE_GATE_MIN_LOOKUPS}); pool more runs",
        }
    else:
        second_round = {
            "recorded": False,
            "reason": "no second-round lookups (the run had no cross-check or compliance "
            "findings to verify, or the summary predates the per-round breakdown)",
        }
        gate = {"min_rate": REUSE_GATE_MIN_RATE, "min_lookups": REUSE_GATE_MIN_LOOKUPS, "passes": None,
                "note": "no second-round lookups"}
    return {
        "recorded": True,
        "policy_version": rollup.get("policy_version"),
        "lookups": lookups,
        "by_status": by_status,
        "by_mode": dict(rollup.get("by_mode") or {}),
        "second_round": second_round,
        "gate": gate,
        "match_rate_all_rounds": _rate(matched, lookups),
        "keyable_rate": _rate(lookups - int(by_status.get(sr.LOOKUP_NOT_KEYABLE, 0) or 0), lookups),
        "passages_matched": passages_matched,
        "passages_matched_in_shadow": int(rollup.get("passages_matched_in_shadow", 0) or 0),
        "passages_per_match": _per_match(passages_matched, matched),
        "findings_supplied": rollup.get("findings_supplied", 0),
        "passages_supplied": rollup.get("passages_supplied", 0),
        "verdicts_accepting_reused_source": rollup.get("verdicts_accepting_reused_source", 0),
        "web_search_requests_when_supplied": rollup.get("web_search_requests_when_supplied", 0),
        "web_search_requests_when_not_supplied": rollup.get("web_search_requests_when_not_supplied", 0),
        "note": "The gate reads the second round only (first-round lookups cannot match). "
        "Search counts are not a comparison; see REUSE_PROTOCOL.",
    }


# ---------------------------------------------------------------------------
# Protocols and promotion criteria (fixed before any run)
# ---------------------------------------------------------------------------

VALIDATION_PROTOCOL: dict[str, Any] = {
    "status": "NOT RUN",
    "question": "Do the validator's concerns identify verdicts a person agrees the quoted evidence does not support, without flagging verdicts it does support?",
    "cost": "Zero extra API spend: observation adds no request. It rides ordinary runs.",
    "steps": [
        "Run ordinary reviews with SPEC_CRITIC_EVIDENCE_VALIDATION=observe (either transport). Keep each run's diagnostics export.",
        "For every listed disagreement, a person reads the finding, the verdict, the quote, and the source, and records one of: validator right, verifier right, both wrong, cannot tell (python -m evals.evidence_validation disagreements EXPORT --markdown).",
        "Draw a random sample of equal size from the assessed verdicts the validator did NOT flag and adjudicate them the same way, blind to the validator's reading. Without it, recall and the false-concern rate cannot be estimated.",
        "Report precision of concerns, recall against the sample, the false-concern rate on supported verdicts, per check and per verdict, with Wilson intervals. Report DISPUTED separately: its direction is the opposite of CONFIRMED/CORRECTED.",
    ],
    "minimum_sample": "100 adjudicated disagreements and 100 adjudicated non-flagged verdicts, across at least two modules.",
    "stopping_rule": "Stop at the minimum sample; do not extend a run to chase a threshold.",
}

REUSE_PROTOCOL: dict[str, Any] = {
    "status": "NOT RUN",
    "question": "Does supplying earlier-retrieved passages to later verifications of the same claim context save searches without changing what verdicts are reached?",
    "stage_1": {
        "name": "shadow measurement",
        "cost": "Zero extra API spend: shadow mode changes no request (either transport).",
        "steps": [
            "Run ordinary reviews with SPEC_CRITIC_SOURCE_REUSE=shadow; keep each diagnostics export.",
            "Read the second-round match rate and the passages per match (python -m evals.evidence_validation reuse EXPORT). First-round lookups are excluded: the store is empty until the first round ends. Both numbers bound what supplying could save.",
        ],
        "gate": "Proceed to stage 2 only if at least 15% of second-round verifications match, over at least 200 second-round lookups (pooled across runs if one run has fewer). Below that, reuse cannot save enough to be worth a paid comparison: record 'rejected — too few matches'. A near-zero passage yield rejects it too: there would be nothing to supply.",
    },
    "stage_2": {
        "name": "paired comparison",
        "cost": "Two real-time runs per spec set: a baseline and SPEC_CRITIC_SOURCE_REUSE=supply. Needs an authorized budget and cap.",
        "steps": [
            "Use the same specs, module, models, and project profile in both arms, real-time transport, each arm with an empty verification cache file of its own (SPEC_CRITIC_CACHE_PATH) so no verdict replays across arms.",
            "Compare, for second-round findings whose context matched: web_search_requests, total verification cost, latency, and the verdict reached.",
            "Every finding whose verdict differs between the arms is adjudicated by a person against its sources; count a support judgment 'degraded' when the reuse arm's verdict is the worse one.",
        ],
        "minimum_sample": "50 matched second-round findings per arm.",
        "stopping_rule": "Stop at the minimum sample or the spending cap, whichever comes first.",
    },
}

PROMOTION_CRITERIA: dict[str, Any] = {
    "validation": {
        "decided_separately_from": "source reuse",
        "observation_stays_on_by_default": "never; it stays a switch until the enforcing question is settled",
        "enforce_any_check_only_if": [
            "that check's concern precision is at least 0.90 (Wilson lower bound at least 0.80) on the adjudicated sample",
            "its false-concern rate on supported verdicts is at most 0.02",
            "and the enforcing rule ships under a new POLICY_VERSION with its own verification-cache namespace, so verdicts cached under the old rule cannot replay around it",
        ],
        "never": "Lexical overlap never becomes an acceptance threshold, whatever it correlates with.",
    },
    "source_reuse": {
        "decided_separately_from": "evidence validation",
        "promote_supply_only_if": [
            "stage 1 passes its gate",
            "stage 2 shows a median saving of at least one web search per matched finding",
            "no support judgment is degraded on adjudication (zero, not a rate)",
            "a batch-transport wiring exists, since batch is the default transport",
        ],
        "otherwise": "Keep the switch off and record the measured match rate and saving.",
    },
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.evidence_validation", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_score = sub.add_parser("score", help="score the validator on the constructed evidence set")
    p_score.add_argument("--split", choices=(ds.SPLIT_TUNING, ds.SPLIT_HELD_OUT, "all"), default=ds.SPLIT_HELD_OUT)
    p_dis = sub.add_parser("disagreements", help="disagreements from a diagnostics export")
    p_dis.add_argument("export")
    p_dis.add_argument("--markdown", action="store_true")
    p_reuse = sub.add_parser("reuse", help="source-reuse measurement from a diagnostics export")
    p_reuse.add_argument("export")
    sub.add_parser("protocol", help="print the protocols and promotion criteria")
    ns = parser.parse_args(argv)
    if ns.command == "score":
        _print(score_split(ns.split))
    elif ns.command == "disagreements":
        table = disagreements(load_summary(ns.export))
        if ns.markdown:
            sys.stdout.write(disagreements_markdown(table))
        else:
            _print(table)
    elif ns.command == "reuse":
        _print(reuse_measurement(load_summary(ns.export)))
    else:
        _print({
            "validation": VALIDATION_PROTOCOL,
            "source_reuse": REUSE_PROTOCOL,
            "promotion": PROMOTION_CRITERIA,
            "recorded_held_out_result": RECORDED_HELD_OUT_RESULT,
        })
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
