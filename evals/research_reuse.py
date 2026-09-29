"""EX-05: reuse a completed requirements-research profile across runs.

The decision record is ``plans/experiments/EX-05-research-reuse.md``; the
cache is :mod:`src.research.research_cache`, switched on with
``SPEC_CRITIC_RESEARCH_CACHE=reuse`` (or ``refresh``). This module holds the
offline half of the experiment and the protocol for the live half:

* ``matrix`` — what the key does with real modules and real request builders:
  for a base project and each single change to one input, whether the key
  stays the same and which components differ. No network, no model.
* ``measure`` — pools the ``research_reuse`` rollups of one or more
  diagnostics exports into the hit rate (with a Wilson interval), the reasons
  for misses, and what the hits saved (dimension calls, API requests,
  searches, tokens, and a lower-bound cost).
* ``diff`` — compares two requirements profiles (a reused one and a fresh one
  of the same inputs, or two fresh ones as the control), so a person can
  adjudicate whether a reuse was inappropriate.
* ``entries`` / ``delete`` — list or remove stored profiles.
* ``protocol`` — the evaluation protocol and the promotion criteria, fixed
  before any run.

Usage::

    python -m evals.research_reuse matrix
    python -m evals.research_reuse measure EXPORT.json [EXPORT.json ...]
    python -m evals.research_reuse diff REUSED.profile.json FRESH.profile.json [--module ID]
    python -m evals.research_reuse entries [--path FILE]
    python -m evals.research_reuse delete (--key KEY ... | --all) [--path FILE]
    python -m evals.research_reuse protocol

``EXPORT.json`` is a diagnostics summary (the Diagnostics window's Save as
JSON, ``scripts/recover_batch.py --diagnostics-json``, or any JSON object with
the summary under ``"summary"``). A ``.profile.json`` is the export written
beside a report (a program's has one profile per module; pass ``--module``),
or a bare serialized profile.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence
from unittest import mock

from evals.model_effort import wilson_interval
from src.core import api_config
from src.core.pricing import estimate_cost_breakdown
from src.core.project_profile import ProjectProfile
from src.modules import get_module
from src.research import research_cache as rc
from src.research import requirements_research as rr

# ---------------------------------------------------------------------------
# The key matrix (offline)
# ---------------------------------------------------------------------------

#: The base scenario every matrix row changes one input of.
BASE_MODULE_ID = "datacenter_fire"
BASE_PROFILE = ProjectProfile(
    city="Ashburn", state_or_province="VA", country="US", client_name="ExampleCo"
)
BASE_CORPUS_BLOCK = (
    "Client/owner documents named in the specifications:\n"
    "- Owner Design Standards Rev 4\n\n"
    "Risk consultant / insurer mentions:\n(none detected)\n\n"
    "Edition-governance language:\n(none detected)\n\n"
    "Standards cited with edition years:\n- NFPA 13 (2022)"
)


def _signals(block: str) -> SimpleNamespace:
    # ``research_reuse_key`` accepts anything with ``render_block()`` — the same
    # duck type the fan-out takes — so the matrix needs no tokenizer.
    return SimpleNamespace(render_block=lambda: block)


@dataclasses.dataclass(frozen=True)
class MatrixCase:
    case_id: str
    description: str
    expect_same_key: bool
    build: Callable[[], dict]


def _base_inputs() -> dict:
    return {
        "module": get_module(BASE_MODULE_ID),
        "profile": BASE_PROFILE,
        "corpus": BASE_CORPUS_BLOCK,
        "model": api_config.RESEARCH_MODEL_DEFAULT,
    }


def _with(**changes) -> Callable[[], dict]:
    def build() -> dict:
        inputs = _base_inputs()
        inputs.update({k: (v() if callable(v) else v) for k, v in changes.items()})
        return inputs

    return build


def _module_with_changed_brief():
    module = get_module(BASE_MODULE_ID)
    first = module.research_dimensions[0]
    changed = dataclasses.replace(first, prompt_template=first.prompt_template + " Also list fees.")
    return dataclasses.replace(
        module, research_dimensions=(changed, *module.research_dimensions[1:])
    )


def _module_with_changed_budget():
    module = get_module(BASE_MODULE_ID)
    first = module.research_dimensions[0]
    changed = dataclasses.replace(first, max_searches=(first.max_searches or 10) + 1)
    return dataclasses.replace(
        module, research_dimensions=(changed, *module.research_dimensions[1:])
    )


def _module_with_changed_persona():
    module = get_module(BASE_MODULE_ID)
    return dataclasses.replace(module, research_persona=module.research_persona + " Be brief.")


MATRIX_CASES: tuple[MatrixCase, ...] = (
    MatrixCase("same_inputs", "The same inputs again", True, _with()),
    MatrixCase(
        "country_alias",
        "Country entered as 'USA' instead of 'US' (a known alias the profile canonicalizes)",
        True,
        _with(profile=dataclasses.replace(BASE_PROFILE, country="USA")),
    ),
    MatrixCase(
        "city_case",
        "City 'ashburn' instead of 'Ashburn' (not assumed equivalent)",
        False,
        _with(profile=dataclasses.replace(BASE_PROFILE, city="ashburn")),
    ),
    MatrixCase(
        "city",
        "Another city in the same state",
        False,
        _with(profile=dataclasses.replace(BASE_PROFILE, city="Leesburg")),
    ),
    MatrixCase(
        "state",
        "Another state",
        False,
        _with(profile=dataclasses.replace(BASE_PROFILE, state_or_province="MD")),
    ),
    MatrixCase(
        "country",
        "Another country",
        False,
        _with(
            profile=ProjectProfile(
                city="Ashburn", state_or_province="ON", country="CA", client_name="ExampleCo"
            )
        ),
    ),
    MatrixCase(
        "client",
        "Another client",
        False,
        _with(profile=dataclasses.replace(BASE_PROFILE, client_name="OtherCo")),
    ),
    MatrixCase(
        "client_case",
        "Client 'Exampleco' instead of 'ExampleCo' (not assumed equivalent)",
        False,
        _with(profile=dataclasses.replace(BASE_PROFILE, client_name="Exampleco")),
    ),
    MatrixCase(
        "module",
        "Another module (electrical) for the same project",
        False,
        _with(module=lambda: get_module("datacenter_electrical")),
    ),
    MatrixCase(
        "corpus_edition",
        "The specifications now cite NFPA 13 (2025)",
        False,
        _with(corpus=BASE_CORPUS_BLOCK.replace("NFPA 13 (2022)", "NFPA 13 (2025)")),
    ),
    MatrixCase(
        "corpus_client_document",
        "The specifications now name Owner Design Standards Rev 5",
        False,
        _with(corpus=BASE_CORPUS_BLOCK.replace("Rev 4", "Rev 5")),
    ),
    MatrixCase(
        "corpus_none",
        "No corpus signals (the scrape found nothing)",
        False,
        _with(corpus=""),
    ),
    MatrixCase(
        "model",
        "Research model overridden to Sonnet 5",
        False,
        _with(model=api_config.MODEL_SONNET_5),
    ),
    MatrixCase(
        "dimension_brief",
        "One dimension's research brief reworded",
        False,
        _with(module=_module_with_changed_brief),
    ),
    MatrixCase(
        "dimension_budget",
        "One dimension's search budget raised",
        False,
        _with(module=_module_with_changed_budget),
    ),
    MatrixCase(
        "persona",
        "The module's research persona reworded",
        False,
        _with(module=_module_with_changed_persona),
    ),
)


def _key_for(inputs: Mapping[str, Any]) -> rc.ResearchKey:
    return rr.research_reuse_key(
        inputs["module"],
        inputs["profile"],
        corpus_signals=_signals(inputs["corpus"]),
        model=inputs["model"],
    )


def key_matrix() -> dict[str, Any]:
    """Every :data:`MATRIX_CASES` row against the base key (no network)."""
    base = _key_for(_base_inputs())
    rows = []
    for case in MATRIX_CASES:
        key = _key_for(case.build())
        same = key.key == base.key
        rows.append(
            {
                "case": case.case_id,
                "description": case.description,
                "same_key": same,
                "expected_same_key": case.expect_same_key,
                "as_expected": same == case.expect_same_key,
                "differing": rc.differing_components(base.components, key.components),
            }
        )
    # Two changes that must not split the key: a deep trace's thinking display
    # and the cache breakpoints' TTL. Both are simulated at the builders.
    with mock.patch.object(api_config, "deep_trace_recording", lambda: True):
        display = _key_for(_base_inputs())
    rows.append(
        {
            "case": "deep_trace_display",
            "description": "A deep trace asks for summarized thinking (visibility only)",
            "same_key": display.key == base.key,
            "expected_same_key": True,
            "as_expected": display.key == base.key,
            "differing": rc.differing_components(base.components, display.components),
        }
    )
    with mock.patch.object(api_config, "_cache_control_block", lambda: {"type": "ephemeral"}):
        ttl = _key_for(_base_inputs())
    rows.append(
        {
            "case": "cache_ttl",
            "description": "The prompt-cache breakpoints' TTL changes (placement only)",
            "same_key": ttl.key == base.key,
            "expected_same_key": True,
            "as_expected": ttl.key == base.key,
            "differing": rc.differing_components(base.components, ttl.components),
        }
    )
    return {
        "base": {
            "module_id": BASE_MODULE_ID,
            "project": BASE_PROFILE.to_dict(),
            "model": api_config.RESEARCH_MODEL_DEFAULT,
            "key": base.key,
            "components": dict(base.components),
        },
        "rows": rows,
        "all_as_expected": all(r["as_expected"] for r in rows),
    }


# ---------------------------------------------------------------------------
# Reading real runs
# ---------------------------------------------------------------------------


def load_summary(path: str | Path) -> dict[str, Any]:
    """A diagnostics summary from a JSON file (bare, or under ``"summary"``)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, Mapping) and isinstance(data.get("summary"), Mapping):
        return dict(data["summary"])
    if not isinstance(data, Mapping):
        raise ValueError(f"{path}: not a JSON object")
    return dict(data)


def _add_counts(into: dict, source: Mapping[str, Any] | None) -> None:
    for key, value in (source or {}).items():
        try:
            into[key] = into.get(key, 0) + int(value)
        except (TypeError, ValueError):
            continue


def saved_cost_lower_bound(saved_by_model: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """What the saved research would have cost, as a lower bound.

    Cache writes are priced at the five-minute rate (1.25x), the cheaper of
    the two: the stored usage does not keep the TTL split, and a saving must
    not be overstated. A model the price table does not know is counted, not
    priced.
    """
    total = 0.0
    unpriced: list[str] = []
    for model, usage in sorted(saved_by_model.items()):
        writes = int(usage.get("cache_creation_input_tokens", 0) or 0)
        breakdown = estimate_cost_breakdown(
            int(usage.get("input_tokens", 0) or 0),
            int(usage.get("output_tokens", 0) or 0),
            model=model,
            cache_creation_input_tokens=writes,
            cache_creation_5m_input_tokens=writes,
            cache_creation_unknown_input_tokens=0,
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            web_search_requests=int(usage.get("web_search_requests", 0) or 0),
        )
        if breakdown is None:
            unpriced.append(model)
            continue
        total += breakdown.total
    return {"usd": round(total, 4), "unpriced_models": unpriced, "basis": "lower bound: cache writes at 1.25x"}


def measure(summaries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pool the ``research_reuse`` rollups of several runs (see the module docstring)."""
    runs = 0
    lookups = hits = rejected = 0
    by_outcome: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    changed: dict[str, int] = {}
    stale: dict[str, int] = {}
    stores: dict[str, int] = {}
    saved: dict[str, int] = {}
    saved_by_model: dict[str, dict[str, int]] = {}
    oldest: int | None = None
    for summary in summaries:
        rollup = summary.get("research_reuse")
        if not isinstance(rollup, Mapping):
            continue
        runs += 1
        lookups += int(rollup.get("lookups", 0) or 0)
        hits += int(rollup.get("hits", 0) or 0)
        rejected += int(rollup.get("rejected_entries", 0) or 0)
        _add_counts(by_outcome, rollup.get("by_outcome"))
        _add_counts(by_mode, rollup.get("by_mode"))
        _add_counts(changed, rollup.get("changed_components"))
        _add_counts(stale, rollup.get("stale_reasons"))
        _add_counts(stores, rollup.get("store_outcomes"))
        _add_counts(saved, rollup.get("saved"))
        for model, usage in (rollup.get("saved_by_model") or {}).items():
            _add_counts(saved_by_model.setdefault(str(model), {}), usage)
        age = rollup.get("oldest_hit_age_days")
        if isinstance(age, int) and not isinstance(age, bool):
            oldest = age if oldest is None else max(oldest, age)
    return {
        "runs_with_research_reuse": runs,
        "lookups": lookups,
        "hits": hits,
        "hit_rate": round(hits / lookups, 4) if lookups else None,
        "hit_rate_wilson95": wilson_interval(hits, lookups),
        "by_outcome": dict(sorted(by_outcome.items())),
        "by_mode": dict(sorted(by_mode.items())),
        "miss_components": dict(sorted(changed.items(), key=lambda kv: (-kv[1], kv[0]))),
        "stale_reasons": dict(sorted(stale.items())),
        "store_outcomes": dict(sorted(stores.items())),
        "rejected_entries": rejected,
        "saved": dict(sorted(saved.items())),
        "saved_cost": saved_cost_lower_bound(saved_by_model),
        "oldest_hit_age_days": oldest,
        "note": (
            "Saved usage is what the stored research cost when it was first run, "
            "not a measurement of the reusing run."
        ),
    }


def load_profile(path: str | Path, *, module_id: str | None = None) -> dict[str, Any]:
    """A serialized requirements profile from a ``.profile.json`` export or a bare dict."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise ValueError(f"{path}: not a JSON object")
    if isinstance(data.get("module_profiles"), Mapping):
        profiles = data["module_profiles"]
        if module_id is None:
            if len(profiles) != 1:
                raise ValueError(
                    f"{path}: a program export; pass --module (one of {sorted(profiles)})"
                )
            module_id = next(iter(profiles))
        data = profiles[module_id]
    if isinstance(data.get("requirements_profile"), Mapping):
        data = data["requirements_profile"]
    return dict(data)


def _controlling(item: Mapping[str, Any]) -> bool:
    return bool(item.get("grounded")) and item.get("actionability", "spec_requirement") != "process_advisory"


def diff_profiles(reused: Mapping[str, Any], fresh: Mapping[str, Any]) -> dict[str, Any]:
    """What differs between two profiles of the same inputs, for adjudication.

    Items are matched by ``item_id``, which is content-addressed (dimension,
    category, and requirement text), so any rewording is a difference. Two
    fresh runs of the same inputs differ too — the model is not deterministic
    — which is why the protocol compares reused-vs-fresh against a
    fresh-vs-fresh control. ``controlling_*`` lists only grounded,
    non-advisory items: the ones compliance treats as requirements.
    """
    a = {str(i.get("item_id")): i for i in reused.get("items") or [] if isinstance(i, Mapping)}
    b = {str(i.get("item_id")): i for i in fresh.get("items") or [] if isinstance(i, Mapping)}
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    both = sorted(set(a) & set(b))
    grounding_changed = [i for i in both if bool(a[i].get("grounded")) != bool(b[i].get("grounded"))]

    def rows(ids, source):
        return [
            {
                "item_id": i,
                "dimension_id": source[i].get("dimension_id"),
                "category": source[i].get("category"),
                "code_reference": source[i].get("code_reference"),
                "requirement": source[i].get("requirement"),
            }
            for i in ids
        ]

    def governing(source):
        return sorted(
            {
                (str(i.get("category")), str(i.get("code_reference") or "").strip().lower())
                for i in source.values()
                if i.get("category") in ("governing_code", "local_amendment", "referenced_standard")
                and _controlling(i)
            }
        )

    gov_a, gov_b = governing(a), governing(b)
    return {
        "reused": {
            "research_date": reused.get("research_date"),
            "reuse": reused.get("reuse"),
            "items": len(a),
        },
        "fresh": {"research_date": fresh.get("research_date"), "items": len(b)},
        "same_project": dict(reused.get("project") or {}) == dict(fresh.get("project") or {}),
        "shared_items": len(both),
        "jaccard": round(len(both) / len(set(a) | set(b)), 4) if (a or b) else None,
        "only_in_reused": rows(only_a, a),
        "only_in_fresh": rows(only_b, b),
        "controlling_only_in_reused": rows([i for i in only_a if _controlling(a[i])], a),
        "controlling_only_in_fresh": rows([i for i in only_b if _controlling(b[i])], b),
        "grounding_changed": rows(grounding_changed, b),
        "governing_references": {
            "only_in_reused": [list(t) for t in gov_a if t not in gov_b],
            "only_in_fresh": [list(t) for t in gov_b if t not in gov_a],
        },
    }


def entries_listing(path: str | Path | None = None) -> dict[str, Any]:
    """The stored profiles, newest first (never their text)."""
    cache = rc.ResearchCache(path)
    entries, rejected, unreadable = cache.entries()
    now = cache.clock()
    return {
        "path": str(cache.path),
        "unreadable": unreadable or None,
        "rejected_rows": rejected,
        "entries": [
            {
                "key": e.key,
                "module_id": e.module_id,
                "project": e.project,
                "research_date": e.research_date,
                "age_days": int(e.age_seconds(now) // 86400),
                "dimensions": len(e.profile.get("dimension_statuses") or []),
                "items": len(e.profile.get("items") or []),
                "usage": e.usage,
            }
            for e in entries
        ],
    }


# ---------------------------------------------------------------------------
# Protocol and promotion criteria (fixed before any run)
# ---------------------------------------------------------------------------

EVALUATION_PROTOCOL: dict[str, Any] = {
    "status": "NOT RUN",
    "question": (
        "On projects reviewed more than once, how often does a stored research profile get reused, "
        "what does that save, and is any reuse inappropriate — a reused requirement that a fresh "
        "research run on the same day would not have given?"
    ),
    "stage_1": {
        "name": "hit rate and saving on ordinary repeated runs",
        "cost": (
            "No extra spend: with SPEC_CRITIC_RESEARCH_CACHE=reuse a miss researches exactly as a "
            "run without the switch would, and a hit makes no research request."
        ),
        "steps": [
            "Point SPEC_CRITIC_RESEARCH_CACHE_PATH at a file of the experiment's own.",
            "Run ordinary reviews of representative repeated projects (the same location, client, and "
            "specifications reviewed again, as happens after an addendum) with "
            "SPEC_CRITIC_RESEARCH_CACHE=reuse, and keep every run's diagnostics export.",
            "python -m evals.research_reuse measure EXPORT.json ... reports the hit rate with its "
            "Wilson interval, the components behind each miss, stale reasons, and the saved calls, "
            "searches, tokens, and lower-bound cost.",
        ],
        "gate": (
            "Proceed to stage 2 only with at least 20 lookups across at least 5 projects and a hit "
            "rate whose Wilson lower bound is at least 0.20. Below that the saving cannot pay for the "
            "applicability risk: record 'rejected - too few hits' with the measured rate."
        ),
    },
    "stage_2": {
        "name": "inappropriate reuse",
        "cost": (
            "One extra fresh research run per sampled hit, plus one fresh-vs-fresh control per "
            "project (SPEC_CRITIC_RESEARCH_CACHE=refresh, which also re-stores). Research only, no "
            "review: needs an authorized budget and cap."
        ),
        "steps": [
            "For each sampled hit, on the same day, run research again with "
            "SPEC_CRITIC_RESEARCH_CACHE=refresh and a separate SPEC_CRITIC_RESEARCH_CACHE_PATH, and "
            "export both profiles (.profile.json).",
            "For each project, also run fresh research twice on one day: the fresh-vs-fresh control "
            "that says how much two runs of the same inputs differ anyway.",
            "python -m evals.research_reuse diff REUSED FRESH lists the controlling items and "
            "governing references only one side has. A person adjudicates each controlling "
            "difference as: changed since the reused research (inappropriate reuse), run-to-run "
            "variation (the control shows the same kind of difference), or cannot tell.",
        ],
        "minimum_sample": "30 adjudicated hits across at least 5 projects and 2 modules.",
        "stopping_rule": "Stop at the minimum sample or the spending cap, whichever comes first.",
    },
}

PROMOTION_CRITERIA: dict[str, Any] = {
    "default_on_only_if": [
        "stage 1 passes its gate",
        "stage 2 finds zero inappropriate reuses among controlling requirements (zero, not a rate), "
        "with the Wilson upper bound of the inappropriate-reuse rate across all adjudicated hits "
        "at most 0.10",
        "the saving is reported in calls, searches, and lower-bound cost, not only as a hit rate",
        "the age limit and date rules stay as measured (a longer limit is a new evaluation)",
    ],
    "never": [
        "reuse a partial or failed profile",
        "treat a calendar month, or any calendar period, as proof the requirements are unchanged",
        "hide a reuse: the log, both reports, and the profile's reuse record always say it happened",
    ],
    "otherwise": "Keep the switch off and record the measured hit rate, saving, and adjudication.",
    "unchanged_by_this_experiment": "SPEC_CRITIC_GOVERNING_BASIS_CONTEXT stays off unless separately evaluated.",
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.research_reuse", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("matrix", help="what the key does with each single changed input")
    p_measure = sub.add_parser("measure", help="hit rate and saving from diagnostics exports")
    p_measure.add_argument("exports", nargs="+")
    p_diff = sub.add_parser("diff", help="compare a reused profile with a fresh one")
    p_diff.add_argument("reused")
    p_diff.add_argument("fresh")
    p_diff.add_argument("--module", default=None)
    p_entries = sub.add_parser("entries", help="list the stored profiles")
    p_entries.add_argument("--path", default=None)
    p_delete = sub.add_parser("delete", help="delete stored profiles")
    group = p_delete.add_mutually_exclusive_group(required=True)
    group.add_argument("--key", action="append")
    group.add_argument("--all", action="store_true")
    p_delete.add_argument("--path", default=None)
    sub.add_parser("protocol", help="print the protocol and promotion criteria")
    ns = parser.parse_args(argv)
    if ns.command == "matrix":
        _print(key_matrix())
    elif ns.command == "measure":
        _print(measure([load_summary(p) for p in ns.exports]))
    elif ns.command == "diff":
        _print(
            diff_profiles(
                load_profile(ns.reused, module_id=ns.module),
                load_profile(ns.fresh, module_id=ns.module),
            )
        )
    elif ns.command == "entries":
        _print(entries_listing(ns.path))
    elif ns.command == "delete":
        cache = rc.ResearchCache(ns.path)
        removed = cache.delete(None if ns.all else ns.key)
        _print({"path": str(cache.path), "deleted": removed})
    else:
        _print({"evaluation": EVALUATION_PROTOCOL, "promotion": PROMOTION_CRITERIA})
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
