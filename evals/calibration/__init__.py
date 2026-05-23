"""Chunk 1 — calibration eval harness.

The Chunk 12 regression harness in :mod:`evals` answers "did the parser /
locator / detector keep doing what it used to do?" — a deterministic
contract check. This sub-package answers a different question: "when the
verifier says CONFIRMED at confidence 0.9, how often is it actually
right?"

Each fixture is a JSON file under :file:`fixtures/` containing:

* the raw review finding (severity / fileName / section / issue / etc.),
* a minimal spec context slice so a human can re-read the surrounding
  paragraph,
* a captured ``VerificationResult`` payload — what the verifier returned
  on a real run, including which URLs the model cited and which URLs the
  web_search tool actually fetched,
* a hand-labeled ground-truth block stating what the verdict *should*
  have been.

The harness reconstructs the ``VerificationResult``, replays the
source-grounding helpers (so an ungrounded CONFIRMED downgrades to
UNVERIFIED in the eval the same way it would in production), runs
:func:`src.output.report_status.classify_status` /
:func:`classify_edit_action`, and hands the per-fixture outcomes to the
scorer.

The scorer emits five tables:

1. Confusion matrix — captured verdict vs. ground-truth verdict.
2. Per-status accuracy — does ``VERIFIED_SUPPORTED`` actually mean the
   finding was right?
3. False-positive auto-edit rate at four ``edit_confidence`` thresholds.
4. Calibration plot — self-reported model confidence bucketed against
   observed correctness rate.
5. Source-grounding integrity — count of CONFIRMED / CORRECTED with and
   without accepted citations.

The harness is intentionally hermetic. ``ANTHROPIC_API_KEY`` is set to a
sentinel value (mirroring :mod:`tests.conftest`) so the eval can run
without network access; fixtures must carry a captured verifier response,
not trigger a live call. Re-recording a fixture against the real API is a
future enhancement, out of scope for Chunk 1.
"""
from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT: Path = Path(__file__).resolve().parent
FIXTURES_DIR: Path = PACKAGE_ROOT / "fixtures"

__all__ = ["PACKAGE_ROOT", "FIXTURES_DIR"]
