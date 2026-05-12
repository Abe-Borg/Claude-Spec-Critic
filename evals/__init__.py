"""Chunk 12 — golden-set evaluation harness.

This package owns the small, repeatable regression suite for Spec Critic.
The goal is to make future prompt/routing/parser changes measurable rather
than judged by vibes:

- :mod:`evals.fixtures` defines the 10-case fixture taxonomy called out
  in the Chunk 12 plan (clean spec, stale code-cycle, placeholder, internal
  contradiction, coordination, valid edit, invalid edit, unsafe DOCX,
  verification-with-source, source-less CONFIRMED).
- :mod:`evals.harness` runs each fixture through production parsers,
  validators, locators, unsafe-markup detectors, and the cost estimator,
  and collects a set of metrics described in the plan (recall, false
  positives, parse failure, edit validity, locator success, unsafe-edit
  refusal, citation acceptance, source-less CONFIRMED, cost availability).
- :mod:`evals.runner` is the CLI entrypoint (``python -m evals.runner``)
  and prints a summary table + per-fixture diff against the checked-in
  baseline in :data:`evals.BASELINE_PATH`.

The harness is intentionally hermetic. Stubbed Anthropic responses from
``tests/fixtures/fake_anthropic.py`` exercise the production parsers
without touching the network; DOCX fixtures are built on the fly in a
caller-supplied tmp directory. A real-network smoke evaluation can be
added later behind a separate flag, but the default suite is offline.
"""
from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT: Path = Path(__file__).resolve().parent

BASELINE_PATH: Path = PACKAGE_ROOT / "baseline.json"
"""Checked-in baseline metrics for the current repaired behavior.

The runner compares every metric against this file and reports drift.
Re-baseline with ``python -m evals.runner --write-baseline`` after an
intentional change.
"""

__all__ = ["PACKAGE_ROOT", "BASELINE_PATH"]
