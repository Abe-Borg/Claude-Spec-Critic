"""EX-06 harness: cross-chunk and cross-module coordination (plan EX-06).

Three jobs, one module:

- ``score`` (offline, free): run the deterministic candidate stage over the
  constructed cases in :mod:`evals.coordination_dataset` and count **missed
  conflicts** (a labeled conflict the stage did not select) and **false
  joins** (a labeled control it did select), with 95% Wilson intervals, plus
  every selected pair that matches no label. The held-out result is pinned in
  :data:`RECORDED_HELD_OUT_RESULT`.
- ``items`` (offline, free): read a diagnostics export (Save as JSON, or
  ``scripts/recover_batch.py --diagnostics-json``) into the table a person
  adjudicates — every candidate or observation with both sides.
- ``run`` (live, **paid**, NOT RUN): run the ``observe`` pass over the
  constructed cases against the real API. It refuses without ``--live``, a
  positive ``--max-spend-usd``, and a real key, and stops at the cap.

The protocol and the promotion criteria are data here
(:data:`EVALUATION_PROTOCOL`, :data:`PROMOTION_CRITERIA`), fixed before any
live result exists. The decision record is
``plans/experiments/EX-06-cross-coordination.md``.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from evals import coordination_dataset as ds

# ---------------------------------------------------------------------------
# Materializing a case
# ---------------------------------------------------------------------------


def _extracted_spec(doc: ds.Doc):
    from src.input.extractor import ExtractedSpec, ParagraphMapping

    mappings = []
    for index, (element_id, heading, text) in enumerate(doc.elements):
        is_row = element_id.startswith("t")
        mappings.append(ParagraphMapping(
            body_index=index,
            element_type="table_cell" if is_row else "paragraph",
            text=text,
            table_index=0 if is_row else None,
            row_index=index if is_row else None,
            cell_index=None,
            element_id=element_id,
            section_id=heading,
        ))
    return ExtractedSpec(
        filename=doc.file_name,
        content="\n\n".join(m.text for m in mappings),
        word_count=sum(len(m.text.split()) for m in mappings),
        paragraph_map=mappings,
    )


def module_inputs(case: ds.Case) -> list:
    """The pass's inputs for a case: one per module, in the case's plan order."""
    from src.coordination.runner import ModuleInput
    from src.modules.registry import get_module

    specs = {doc.file_name: _extracted_spec(doc) for doc in case.docs}
    inputs = []
    for module_id, groups in case.plans:
        files = [doc.file_name for doc in case.docs if module_id in doc.modules]
        inputs.append(ModuleInput(
            module_id=module_id,
            display_name=get_module(module_id).display_name,
            specs=tuple(specs[name] for name in files),
            chunk_groups=tuple(frozenset(group) for group in groups),
            cross_check_status="completed",
            cycle_label=get_module(module_id).cycle.label,
        ))
    return inputs


# ---------------------------------------------------------------------------
# Offline scoring
# ---------------------------------------------------------------------------


def wilson_interval(successes: int, n: int, z: float = 1.959964) -> list[float] | None:
    """The 95% Wilson score interval for ``successes / n`` (``None`` when n is 0)."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _rate(count: int, n: int) -> dict[str, Any]:
    return {
        "count": count,
        "n": n,
        "rate": round(count / n, 4) if n else None,
        "wilson95": wilson_interval(count, n),
    }


def _sides(candidate) -> frozenset:
    return frozenset({
        (candidate.side_a.file_name, candidate.side_a.element_id),
        (candidate.side_b.file_name, candidate.side_b.element_id),
    })


def score_case(case: ds.Case) -> dict[str, Any]:
    """Run the candidate stage over one case and match it to the labels."""
    from src.coordination.runner import run_coordination

    result = run_coordination(module_inputs(case), mode="candidates", scope=case.scope)
    joined = list(result.selection.selected) + [c for c, _why in result.selection.deferred]
    matched: set[str] = set()
    labels = [(pair, "conflict") for pair in case.conflicts] + [
        (pair, "control") for pair in case.controls
    ]
    hit_labels: dict[str, list[str]] = {}
    for candidate in joined:
        key = _sides(candidate)
        for pair, _kind in labels:
            if key == frozenset({pair.a, pair.b}) and candidate.category == pair.category:
                hit_labels.setdefault(pair.pair_id, []).append(candidate.candidate_id)
                matched.add(candidate.candidate_id)
    return {
        "case_id": case.case_id,
        "split": case.split,
        "status": result.status,
        "missed_conflicts": [p.pair_id for p in case.conflicts if p.pair_id not in hit_labels],
        "found_conflicts": [p.pair_id for p in case.conflicts if p.pair_id in hit_labels],
        "false_joins": [p.pair_id for p in case.controls if p.pair_id in hit_labels],
        "held_controls": [p.pair_id for p in case.controls if p.pair_id not in hit_labels],
        "unlabeled_candidates": sorted(c.candidate_id for c in joined if c.candidate_id not in matched),
        "joined": len(joined),
    }


def score(split: str | None = ds.SPLIT_HELD_OUT) -> dict[str, Any]:
    """The candidate stage over a split: missed conflicts and false joins."""
    selected = ds.cases(split)
    per_case = [score_case(case) for case in selected]
    conflicts = sum(len(c.conflicts) for c in selected)
    controls = sum(len(c.controls) for c in selected)
    missed = sorted(p for r in per_case for p in r["missed_conflicts"])
    false = sorted(p for r in per_case for p in r["false_joins"])
    unlabeled = sum(len(r["unlabeled_candidates"]) for r in per_case)
    return {
        "split": split or "all",
        "policy_version": _policy_version(),
        "dataset_digest": ds.dataset_digest(split),
        "cases": len(selected),
        "conflicts": conflicts,
        "controls": controls,
        "conflict_recall": _rate(conflicts - len(missed), conflicts),
        "missed_conflicts": missed,
        "false_join_rate": _rate(len(false), controls),
        "false_joins": false,
        "unlabeled_candidates": unlabeled,
        "per_case": per_case,
    }


def _policy_version() -> str:
    from src.coordination.facts import POLICY_VERSION

    return POLICY_VERSION


#: The held-out split scored once, with the rules frozen at ``cx1``. A test
#: reproduces it exactly: a rule change that moves it is a new policy version
#: and needs new held-out cases.
RECORDED_HELD_OUT_RESULT: dict[str, Any] = {}


def recorded_view(result: Mapping[str, Any]) -> dict[str, Any]:
    """The fields of a :func:`score` result that :data:`RECORDED_HELD_OUT_RESULT` pins."""
    return {
        "policy_version": result["policy_version"],
        "dataset_digest": result["dataset_digest"],
        "cases": result["cases"],
        "conflicts": result["conflicts"],
        "controls": result["controls"],
        "conflicts_found": result["conflict_recall"]["count"],
        "missed_conflicts": list(result["missed_conflicts"]),
        "false_joins": list(result["false_joins"]),
        "unlabeled_candidates": result["unlabeled_candidates"],
    }


# ---------------------------------------------------------------------------
# Reading a diagnostics export
# ---------------------------------------------------------------------------


def items_from_export(export: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every coordination candidate / observation a diagnostics export holds.

    Reads the summary rollup when present, else the events themselves.
    """
    summary = export.get("summary") if isinstance(export, Mapping) else None
    rollup = (summary or {}).get("coordination") if isinstance(summary, Mapping) else None
    if isinstance(rollup, Mapping) and isinstance(rollup.get("items"), list):
        return [dict(item) for item in rollup["items"] if isinstance(item, Mapping)]
    items: list[dict[str, Any]] = []
    for event in export.get("events", []) if isinstance(export, Mapping) else []:
        data = event.get("data") if isinstance(event, Mapping) else None
        if isinstance(data, Mapping) and isinstance(data.get("coordination_item"), Mapping):
            items.append(dict(data["coordination_item"]))
    return items


def adjudication_rows(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per item for a person to adjudicate (both sides, the judgment,
    and blank columns for the person's reading)."""
    rows = []
    for item in items:
        sides = list(item.get("sides") or [{}, {}]) + [{}, {}]
        observation = item.get("observation") or {}
        rows.append({
            "candidate_id": item.get("candidate_id", ""),
            "kind": item.get("kind", ""),
            "category": item.get("category", ""),
            "attribute": item.get("attribute", ""),
            "subject": item.get("subject_display") or item.get("subject", ""),
            "side_a": f"{sides[0].get('file_name', '')} {sides[0].get('element_id', '')}: "
                      f"{sides[0].get('raw_value', '')}",
            "passage_a": sides[0].get("passage", ""),
            "side_b": f"{sides[1].get('file_name', '')} {sides[1].get('element_id', '')}: "
                      f"{sides[1].get('raw_value', '')}",
            "passage_b": sides[1].get("passage", ""),
            "model_assessment": observation.get("assessment", "") if observation else "",
            "validation": observation.get("validation", "") if observation else "",
            "same_scope_reason": observation.get("same_scope_reason", "") if observation else "",
            "person_same_item": "",
            "person_conflict": "",
        })
    return rows


def rows_markdown(rows: Sequence[Mapping[str, Any]]) -> str:
    headers = ["candidate_id", "kind", "category", "subject", "side_a", "side_b",
               "model_assessment", "person_same_item", "person_conflict"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows:
        cells = [str(row.get(h, "")).replace("|", "\\|").replace("\n", " ") for h in headers]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Live run (paid; NOT RUN)
# ---------------------------------------------------------------------------

_SENTINEL_KEY = "test-key-not-real-do-not-use"


class RunRefused(RuntimeError):
    """A paid run's preconditions do not hold; nothing was sent."""


def check_run_preconditions(*, live: bool, max_spend_usd: float | None,
                            env: Mapping[str, str] | None = None) -> None:
    env = dict(os.environ if env is None else env)
    problems: list[str] = []
    if not live:
        problems.append("a run sends paid requests; pass --live to confirm")
    if max_spend_usd is None or not (max_spend_usd > 0):
        problems.append("a positive spending cap (--max-spend-usd) is required")
    key = env.get("ANTHROPIC_API_KEY", "")
    if not key or key == _SENTINEL_KEY:
        problems.append("no real ANTHROPIC_API_KEY in the environment")
    if problems:
        raise RunRefused("; ".join(problems))


def price_attempts(attempts: Iterable[Mapping[str, Any]]) -> float:
    """Estimated USD of known attempts, each on its own model and transport."""
    from src.core.api_config import cache_pricing_kwargs
    from src.core.attempt_usage import AttemptUsage
    from src.core.pricing import estimate_cost_breakdown

    total = 0.0
    for raw in attempts:
        attempt = raw if isinstance(raw, AttemptUsage) else AttemptUsage.from_dict(raw)
        if not attempt.usage_known:
            continue
        cost = estimate_cost_breakdown(
            attempt.input_tokens, attempt.output_tokens, model=attempt.model,
            batch=attempt.transport == "batch",
            web_search_requests=attempt.web_search_requests,
            **cache_pricing_kwargs(attempt),
        )
        if cost is not None:
            total += cost.total
    return round(total, 6)


def run_live(*, split: str, out_path: Path, live: bool, max_spend_usd: float | None,
             client: Any = None, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Run the ``observe`` pass over a split's cases; stop at the cap.

    Writes one JSON line per case (the pass's summary and item records) to
    ``out_path``. Refuses to start without the preconditions; refuses to
    overwrite ``out_path``.
    """
    check_run_preconditions(live=live, max_spend_usd=max_spend_usd, env=env)
    if out_path.exists():
        raise RunRefused(f"{out_path} exists; use a new output file")
    from src.coordination.runner import run_coordination

    spent = 0.0
    done = 0
    stopped = ""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for case in ds.cases(split):
            if spent >= float(max_spend_usd):
                stopped = f"spending cap reached before {case.case_id}"
                break
            result = run_coordination(
                module_inputs(case), mode="observe", scope=case.scope, client=client
            )
            spent += price_attempts(result.call_usage)
            done += 1
            handle.write(json.dumps({
                "case_id": case.case_id,
                "summary": result.to_dict(),
                "items": result.item_records(),
                "usd": price_attempts(result.call_usage),
            }, ensure_ascii=False) + "\n")
    return {"cases_run": done, "usd": round(spent, 6), "stopped": stopped, "out": str(out_path)}


# ---------------------------------------------------------------------------
# Protocol and promotion criteria (fixed before any live result)
# ---------------------------------------------------------------------------

EVALUATION_PROTOCOL: dict[str, Any] = {
    "status": "NOT RUN",
    "stages": [
        {
            "stage": "0 — candidates on ordinary runs",
            "spend": "none (SPEC_CRITIC_CROSS_COORDINATION=candidates adds no request)",
            "setting": "candidates mode, module scope, then program scope",
            "measure": [
                "candidates joined, selected, and deferred per run, by kind and category",
                "facts per specification and values with no subject (unattributed)",
                "a person adjudicates every selected candidate from the diagnostics "
                "export (python -m evals.coordination items EXPORT --markdown): same "
                "item and scope, yes / no / cannot tell; conflict, yes / no",
            ],
            "gate": "at least 30 adjudicated candidates per category before stage 1 "
                    "spends on that category",
        },
        {
            "stage": "1 — observe within one module",
            "spend": "authorized budget only",
            "setting": "observe mode, module scope; the constructed cases (python -m "
                       "evals.coordination run --live) and real packages whose "
                       "cross-check chunked",
            "measure": [
                "missed conflicts: labeled conflicts judged anything but conflict",
                "false joins: controls, and person-adjudicated non-conflicts, judged conflict",
                "cannot_tell rate; validation demotions (a conflict without both quotes)",
                "incremental cost and latency per pass, from the attempt records",
            ],
        },
        {
            "stage": "2 — observe for a small program",
            "spend": "authorized budget only",
            "setting": "observe mode, program scope, a hyperscale package of 5-15 "
                       "specifications across at least two modules",
            "measure": "as stage 1, reported separately for cross_module candidates",
        },
    ],
    "sample_minimum": "50 adjudicated observations per category and stage",
    "stopping_rule": "stop at the spending cap or the sample minimum, whichever first",
    "results_artifact": "one JSON line per case (run) or the diagnostics export (ordinary runs)",
}

PROMOTION_CRITERIA: dict[str, Any] = {
    "unit": "one category (responsibility, electrical, rating) at a time",
    "observation_never_a_default": True,
    "to_report_a_category": [
        "stage 1 and stage 2 both run for the category",
        "false-join rate on conflict judgments <= 0.10 with a 95% Wilson upper bound <= 0.20",
        "no controlled false join of a different phase, building, or existing/new item",
        "missed-conflict rate on labeled conflicts reported (no threshold: the candidate "
        "stage bounds recall, and the report must say what is not compared)",
        "each reported conflict's two sides verified under its own module's governing "
        "basis — built and tested first; nothing verifies an observation today",
        "incremental cost per run measured and stated in the decision record",
    ],
    "never": [
        "promote the whole pass at once",
        "claim complete coordination: the report must keep naming what was not compared",
        "let a lossy digest replace the per-specification review",
    ],
}


def describe() -> dict[str, Any]:
    return {
        "policy_version": _policy_version(),
        "dataset": {
            "tuning": {"cases": len(ds.cases(ds.SPLIT_TUNING)),
                       "digest": ds.dataset_digest(ds.SPLIT_TUNING)},
            "held_out": {"cases": len(ds.cases(ds.SPLIT_HELD_OUT)),
                         "digest": ds.dataset_digest(ds.SPLIT_HELD_OUT)},
        },
        "protocol": EVALUATION_PROTOCOL,
        "promotion": PROMOTION_CRITERIA,
        "recorded_held_out": RECORDED_HELD_OUT_RESULT,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.coordination", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_score = sub.add_parser("score", help="score the candidate stage offline")
    p_score.add_argument("--split", choices=[*ds.SPLITS, "all"], default=ds.SPLIT_HELD_OUT)
    p_score.add_argument("--json", action="store_true")
    p_items = sub.add_parser("items", help="read a diagnostics export into an adjudication table")
    p_items.add_argument("export", type=Path)
    p_items.add_argument("--markdown", action="store_true")
    p_run = sub.add_parser("run", help="PAID: observe the constructed cases (NOT RUN)")
    p_run.add_argument("--split", choices=ds.SPLITS, default=ds.SPLIT_HELD_OUT)
    p_run.add_argument("--out", type=Path, required=True)
    p_run.add_argument("--max-spend-usd", type=float)
    p_run.add_argument("--live", action="store_true")
    sub.add_parser("describe", help="print the protocol, criteria, and dataset digests")
    args = parser.parse_args(argv)

    if args.command == "score":
        result = score(None if args.split == "all" else args.split)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            recall = result["conflict_recall"]
            false = result["false_join_rate"]
            print(f"split={result['split']} cases={result['cases']} policy={result['policy_version']}")
            print(f"conflicts found {recall['count']}/{recall['n']} (95% {recall['wilson95']}); "
                  f"missed: {', '.join(result['missed_conflicts']) or 'none'}")
            print(f"false joins {false['count']}/{false['n']} (95% {false['wilson95']}): "
                  f"{', '.join(result['false_joins']) or 'none'}")
            print(f"unlabeled candidates: {result['unlabeled_candidates']}")
        return 0
    if args.command == "items":
        export = json.loads(args.export.read_text(encoding="utf-8"))
        rows = adjudication_rows(items_from_export(export))
        print(rows_markdown(rows) if args.markdown else json.dumps(rows, indent=2))
        return 0
    if args.command == "run":
        try:
            outcome = run_live(split=args.split, out_path=args.out, live=args.live,
                               max_spend_usd=args.max_spend_usd)
        except RunRefused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(outcome, indent=2))
        return 0
    print(json.dumps(describe(), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "EVALUATION_PROTOCOL",
    "PROMOTION_CRITERIA",
    "RECORDED_HELD_OUT_RESULT",
    "RunRefused",
    "adjudication_rows",
    "check_run_preconditions",
    "describe",
    "recorded_view",
    "items_from_export",
    "module_inputs",
    "price_attempts",
    "run_live",
    "rows_markdown",
    "score",
    "score_case",
    "wilson_interval",
]
