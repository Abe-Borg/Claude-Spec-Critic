"""Spec Critic eval harnesses.

Two complementary harnesses live under this package:

- :mod:`evals.runner` — the **regression** harness. Walks the
  fixture taxonomy in :mod:`evals.fixtures` through the production
  parsers and source-grounding helpers. The metric story is "did the
  parser / detector keep doing what it used to do?" — drift signals a
  regression.

- :mod:`evals.calibration` — the **calibration** harness.
  Replays hand-labeled JSON fixtures through the production grounding
  + classification helpers and scores the pipeline's verdicts /
  statuses against ground-truth labels. The metric story is "is the
  pipeline *correct*?" Drift here is intentional — later tuning should
  push numbers in a better direction.

Both harnesses are hermetic. The Chunk 12 runner uses stubbed Anthropic
responses from :mod:`tests.fixtures.fake_anthropic` and DOCX fixtures
built on the fly. The Chunk 1 runner replays captured verifier
responses — fixtures carry the verdict the verifier returned, and the
harness re-runs grounding + classification without touching the
network.
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
