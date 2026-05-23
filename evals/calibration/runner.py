"""CLI entrypoint for the calibration eval.

Usage::

    python -m evals.calibration.runner            # print markdown report
    python -m evals.calibration.runner --json     # machine-readable view
    python -m evals.calibration.runner --output report.md   # write to file

Exit codes:

* ``0`` — every fixture's classifier outcomes matched the ground truth.
* ``1`` — at least one fixture's verdict / status / edit-action did not
  match. The markdown report enumerates which fixtures and why.
* ``2`` — fixture loading failed (missing required key, invalid verdict
  spelling, duplicate fixture_id).

The runner is hermetic: it sets a sentinel ``ANTHROPIC_API_KEY`` before
importing production modules (mirroring :mod:`tests.conftest`) so a CI
environment without secrets can still run the eval.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

from . import FIXTURES_DIR


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.calibration.runner",
        description=(
            "Replay hand-labeled fixtures through the production grounding +"
            " classification helpers and emit a scoring report."
        ),
    )
    parser.add_argument(
        "--fixtures-dir",
        default=str(FIXTURES_DIR),
        help="Directory containing fixture .json files. Defaults to the"
        " checked-in calibration fixtures.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional path to write the rendered report to. The report is"
        " always printed to stdout regardless.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the scoring report as JSON instead of markdown.",
    )
    return parser.parse_args(argv)


def _outcomes_to_dicts(report) -> list[dict]:
    out: list[dict] = []
    for o in report.outcomes:
        out.append(dataclasses.asdict(o))
    return out


def _report_to_dict(report) -> dict:
    return {
        "total_fixtures": report.total_fixtures,
        "verdict_correct": report.verdict_correct,
        "verdict_accuracy_rate": report.overall_verdict_accuracy,
        "fixture_pass": report.fixture_pass,
        "fixture_fail": report.fixture_fail,
        # Chunk 13 / Trust Upgrade.
        "budget_exhausted_count": report.budget_exhausted_count,
        "confusion_matrix": {
            "rows": [
                {
                    "expected": row.expected,
                    "counts": row.counts,
                    "row_total": row.row_total,
                    "correct": row.correct,
                    "recall": row.recall,
                }
                for row in report.confusion_matrix.rows
            ],
            "column_totals": report.confusion_matrix.column_totals,
            "column_correct": report.confusion_matrix.column_correct,
        },
        "status_accuracy": [
            {
                "status": s.status,
                "assigned": s.assigned,
                "expected_count": s.expected_count,
                "correct": s.correct,
                "precision": s.precision,
                "recall": s.recall,
            }
            for s in report.status_accuracy
        ],
        "auto_edit_false_positive": [
            {
                "threshold": row.threshold,
                "auto_edit_count": row.auto_edit_count,
                "correct_count": row.correct_count,
                "incorrect_count": row.incorrect_count,
                "false_positive_rate": row.false_positive_rate,
            }
            for row in report.auto_edit_fp
        ],
        "calibration": [
            {
                "label": b.label,
                "lower": b.lower,
                "upper": b.upper,
                "n": b.n,
                "correct": b.correct,
                "correctness_rate": b.correctness_rate,
            }
            for b in report.calibration
        ],
        "grounding_integrity": dataclasses.asdict(report.grounding_integrity),
        "outcomes": _outcomes_to_dicts(report),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Production modules read ``ANTHROPIC_API_KEY`` at import time. Mirror
    # the test conftest's sentinel so a CI runner without secrets configured
    # can still execute this harness.
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real-do-not-use")

    # Import production-dependent modules *after* the env var is set.
    from .harness import run_harness
    from .loader import find_duplicate_ids, load_all_fixtures
    from .scorer import render_markdown, score

    fixtures_dir = Path(args.fixtures_dir)
    try:
        fixtures = load_all_fixtures(fixtures_dir)
    except (ValueError, FileNotFoundError) as exc:
        sys.stderr.write(f"Fixture load failed: {exc}\n")
        return 2

    duplicates = find_duplicate_ids(fixtures)
    if duplicates:
        sys.stderr.write(
            "Duplicate fixture_id(s): " + ", ".join(duplicates) + "\n"
        )
        return 2

    if not fixtures:
        sys.stderr.write(
            f"No fixtures found under {fixtures_dir}. Add at least one .json"
            " fixture before running the eval.\n"
        )
        return 2

    harness_result = run_harness(fixtures)
    report = score(harness_result)

    if args.json:
        rendered = json.dumps(_report_to_dict(report), indent=2, sort_keys=True) + "\n"
    else:
        rendered = render_markdown(report)

    sys.stdout.write(rendered)
    if not rendered.endswith("\n"):
        sys.stdout.write("\n")

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")

    return 0 if report.fixture_fail == 0 else 1


if __name__ == "__main__":  # pragma: no cover — invoked via __main__
    sys.exit(main())
