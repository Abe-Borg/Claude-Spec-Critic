"""CLI entrypoint for the golden-set eval harness.

Run with ``python -m evals.runner`` to:

* execute every fixture from :mod:`evals.fixtures`,
* compute the ten Chunk 12 metrics,
* render a summary table + per-fixture pass/fail report,
* compare metrics against the checked-in baseline at
  :data:`evals.BASELINE_PATH` and report any drift.

Exit codes:

* ``0`` — all fixtures passed and (when --compare is in effect) every
  metric matched the baseline.
* ``1`` — at least one fixture failed.
* ``2`` — fixtures all passed but a tracked metric drifted from the
  baseline; the caller likely needs to re-baseline after an intentional
  change with ``--write-baseline``.

The runner has no API-key dependency and is safe to invoke in CI.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import BASELINE_PATH
from .harness import FixtureResult, HarnessResult, run_harness


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


_METRIC_DISPLAY_ORDER = (
    ("review_recall", "Review recall"),
    ("false_positive_count", "False positives (clean specs)"),
    ("duplicate_finding", "Duplicate findings"),
    ("parse_failure", "Parse failures"),
    ("edit_proposal_validity", "Edit proposal validity"),
    ("locator_success", "Locator success"),
    ("unsafe_edit_refusal", "Unsafe-edit refusal"),
    ("citation_acceptance", "Citation acceptance"),
    ("sourceless_confirmed", "Sourceless CONFIRMED survivors"),
    ("cost_estimate", "Cost estimate"),
)


def _fmt_rate(metric: dict) -> str:
    rate = metric.get("rate", 0.0)
    return f"{rate:.4f} ({metric.get('numerator', 0)}/{metric.get('denominator', 0)})"


def _fmt_cost(cost: dict) -> str:
    if not cost.get("available"):
        return "unavailable"
    total = cost.get("total_usd", 0.0)
    phases = ", ".join(cost.get("phases") or ())
    return f"${total:.4f} across [{phases}]"


def render_summary(result: HarnessResult) -> str:
    """Return a readable summary block — header / metrics / per-fixture status."""
    metrics = result.metrics
    lines: list[str] = []
    lines.append("Spec Critic golden-set eval summary")
    lines.append("=" * 72)
    lines.append(
        f"Fixtures: {metrics.get('fixture_count', 0)} "
        f"(pass={metrics.get('fixture_pass_count', 0)}, "
        f"fail={metrics.get('fixture_fail_count', 0)})"
    )
    lines.append("")
    lines.append("Metric                                | Value")
    lines.append("-" * 72)
    for key, label in _METRIC_DISPLAY_ORDER:
        if key == "false_positive_count":
            value = str(metrics.get("false_positive_count", 0))
        elif key == "cost_estimate":
            value = _fmt_cost(metrics.get("cost_estimate") or {})
        else:
            value = _fmt_rate(metrics.get(key) or {})
        lines.append(f"{label:<38}| {value}")
    lines.append("")
    lines.append("Per-fixture status")
    lines.append("-" * 72)
    for fr in result.fixtures:
        marker = "PASS" if fr.passed else "FAIL"
        lines.append(f"[{marker}] {fr.fixture_id:<40} ({fr.category})")
        if not fr.passed:
            for issue in fr.issues:
                lines.append(f"        - {issue}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Baseline IO + comparison
# ---------------------------------------------------------------------------


def _result_to_dict(result: HarnessResult) -> dict[str, Any]:
    """JSON-serializable view of a :class:`HarnessResult`."""
    return {
        "metrics": result.metrics,
        "fixtures": [
            dataclasses.asdict(fr) for fr in result.fixtures
        ],
    }


def write_baseline(result: HarnessResult, path: Path = BASELINE_PATH) -> None:
    """Persist the current run's metrics + per-fixture counts as the baseline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _result_to_dict(result)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# Metric keys that ship with a rate component — compared as floats.
_RATE_KEYS = (
    "review_recall",
    "duplicate_finding",
    "parse_failure",
    "edit_proposal_validity",
    "locator_success",
    "unsafe_edit_refusal",
    "citation_acceptance",
    "sourceless_confirmed",
)


def compare_to_baseline(
    result: HarnessResult, baseline: dict[str, Any]
) -> list[str]:
    """Return a list of drift messages (empty when the run matches the baseline)."""
    drift: list[str] = []
    baseline_metrics = (baseline or {}).get("metrics") or {}
    current_metrics = result.metrics

    for key in _RATE_KEYS:
        cur = (current_metrics.get(key) or {}).get("rate")
        base = (baseline_metrics.get(key) or {}).get("rate")
        if cur is None or base is None:
            continue
        if cur != base:
            drift.append(f"{key}: baseline rate {base} vs current {cur}")

    cur_fp = current_metrics.get("false_positive_count")
    base_fp = baseline_metrics.get("false_positive_count")
    if cur_fp is not None and base_fp is not None and cur_fp != base_fp:
        drift.append(
            f"false_positive_count: baseline {base_fp} vs current {cur_fp}"
        )

    cur_cost = (current_metrics.get("cost_estimate") or {}).get("available")
    base_cost = (baseline_metrics.get("cost_estimate") or {}).get("available")
    if cur_cost is not None and base_cost is not None and cur_cost != base_cost:
        drift.append(
            f"cost_estimate.available: baseline {base_cost} vs current {cur_cost}"
        )

    cur_pass = current_metrics.get("fixture_pass_count")
    base_pass = baseline_metrics.get("fixture_pass_count")
    if cur_pass is not None and base_pass is not None and cur_pass != base_pass:
        drift.append(
            f"fixture_pass_count: baseline {base_pass} vs current {cur_pass}"
        )

    return drift


# ---------------------------------------------------------------------------
# Argument parsing + main
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.runner",
        description="Run the Spec Critic golden-set eval harness.",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Overwrite the checked-in baseline with the current run's metrics.",
    )
    parser.add_argument(
        "--no-compare",
        action="store_true",
        help="Skip comparison against the checked-in baseline.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of the readable summary.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # The runner must be importable on a clean environment; the production
    # modules read ``ANTHROPIC_API_KEY`` at import time. Mirror the test
    # conftest's placeholder so a CI runner without secrets configured can
    # still execute the harness.
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real-do-not-use")

    result = run_harness()

    if args.json:
        sys.stdout.write(
            json.dumps(_result_to_dict(result), indent=2, sort_keys=True) + "\n"
        )
    else:
        sys.stdout.write(render_summary(result))

    if args.write_baseline:
        write_baseline(result)
        sys.stdout.write(f"\nBaseline updated: {BASELINE_PATH}\n")

    exit_code = 0
    if result.metrics.get("fixture_fail_count", 0):
        exit_code = 1

    if not args.no_compare and not args.write_baseline:
        baseline = load_baseline()
        if baseline is not None:
            drift = compare_to_baseline(result, baseline)
            if drift:
                sys.stdout.write("\nBaseline drift detected:\n")
                for line in drift:
                    sys.stdout.write(f"  - {line}\n")
                sys.stdout.write(
                    "\nRe-baseline with `python -m evals.runner --write-baseline` "
                    "after an intentional change.\n"
                )
                if exit_code == 0:
                    exit_code = 2
        else:
            sys.stdout.write(
                f"\nNo baseline found at {BASELINE_PATH}; "
                "use --write-baseline to create one.\n"
            )

    return exit_code


if __name__ == "__main__":  # pragma: no cover — invoked via __main__
    sys.exit(main())
