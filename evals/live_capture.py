"""Live-capture harness: run the REAL prompts and feed the existing scorers.

Both hermetic harnesses (:mod:`evals.harness`, :mod:`evals.calibration`)
replay captured model output, so neither can observe a prompt change. This
module closes that gap: with a real ``ANTHROPIC_API_KEY`` and ``--live`` it
runs the production review + verification prompts over the small labeled
set in :mod:`evals.labeled_specs`, scores the review findings against the
labels (recall / false positives / severity match), and writes one
calibration fixture per matched defect in the **existing**
:class:`evals.calibration.loader.CalibrationFixture` JSON shape. The
unchanged ``python -m evals.calibration.runner --fixtures-dir <out>`` then
grades verdict accuracy, confidence calibration, and grounding integrity.

Defect matching on the live path goes through the LLM-as-judge matcher
(:mod:`evals.judge`): one Haiku-class call per spec decides which finding
identifies each labeled defect (phrasing-robust, unlike the substring
default), and a second call classifies extra findings as legitimate /
duplicate / hallucination. Any judge failure falls back to the substring
matcher for that spec and the per-spec report says which matcher decided.
``--no-judge`` restores pure substring matching.

Safety / cost:

* **Hermetic by default.** Without ``--live`` it prints a notice and exits
  0, so CI and the default test run never touch the network.
* ``--live`` refuses to run against the test sentinel key (mirrors
  ``tests/conftest.py``), so an accidental run in a test environment fails
  fast instead of 401-ing mid-capture.
* Verification runs with ``cache=None`` so a stale cached verdict cannot
  mask the reframed verifier prompt (the cache key omits the prompt).
* Only findings that match a labeled defect are verified, keeping the
  search spend bounded. Judge calls are Haiku-class and add cents per run.

The emitted ground truth is *seeded* from the label (and the captured
verdict as a fallback) and flagged for human review — re-recording is a
capture, not an oracle; a human still confirms ``correct_verdict``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .labeled_specs import (
    LABELED_SPECS,
    ExpectedDefect,
    LabeledSpec,
    SpecReviewScore,
    defect_matched,
    score_spec_review,
)

# Mirrors tests/conftest.py — a key equal to this is the hermetic sentinel,
# never a real credential.
_SENTINEL_KEY = "test-key-not-real-do-not-use"

# Verdicts the calibration loader accepts; a seeded ground truth must be one
# of these or the fixture fails to load.
_VALID_VERDICTS = frozenset({"CONFIRMED", "CORRECTED", "DISPUTED", "UNVERIFIED"})

LogFn = Callable[..., None]


def _default_out_dir() -> Path:
    """Default capture output dir — sibling of the hand-labeled fixtures.

    Writing here (not the canonical ``fixtures/`` dir) keeps a live run from
    clobbering the curated set; point the runner at it explicitly with
    ``--fixtures-dir``.
    """
    return Path(__file__).resolve().parent / "calibration" / "fixtures_live"


def real_key_present() -> bool:
    """True when a non-sentinel ``ANTHROPIC_API_KEY`` is configured."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return bool(key) and key != _SENTINEL_KEY


# ---------------------------------------------------------------------------
# Serialization (pure — no model, no network; unit-tested hermetically).
# ---------------------------------------------------------------------------


def _finding_payload(finding: Any) -> dict:
    return {
        "severity": getattr(finding, "severity", ""),
        "fileName": getattr(finding, "fileName", ""),
        "section": getattr(finding, "section", ""),
        "issue": getattr(finding, "issue", ""),
        "actionType": getattr(finding, "actionType", ""),
        "existingText": getattr(finding, "existingText", None),
        "replacementText": getattr(finding, "replacementText", None),
        "codeReference": getattr(finding, "codeReference", None),
        "confidence": getattr(finding, "confidence", 0.5),
        "anchorText": getattr(finding, "anchorText", None),
        "insertPosition": getattr(finding, "insertPosition", None),
        "evidenceElementId": getattr(finding, "evidenceElementId", None),
    }


def _verifier_payload(result: Any) -> dict:
    return {
        "verdict": getattr(result, "verdict", "UNVERIFIED"),
        "explanation": getattr(result, "explanation", ""),
        "sources": list(getattr(result, "sources", []) or []),
        "correction": getattr(result, "correction", None),
        "model_used": getattr(result, "model_used", ""),
        "verification_mode": getattr(result, "verification_mode", ""),
        "verification_profile": getattr(result, "verification_profile", ""),
        "web_search_requests": getattr(result, "web_search_requests", 0),
        "successful_source_count": getattr(result, "successful_source_count", 0),
        "search_error_count": getattr(result, "search_error_count", 0),
        # The loader expects ``searched_urls`` — the result calls them
        # ``searched_sources`` (the URLs web_search actually fetched).
        "searched_urls": list(getattr(result, "searched_sources", []) or []),
        "grounded": bool(getattr(result, "grounded", False)),
        "cache_status": getattr(result, "cache_status", "miss"),
    }


def build_fixture_dict(
    spec: LabeledSpec,
    finding: Any,
    result: Any,
    defect: ExpectedDefect | None,
    *,
    cycle_label: str,
    index: int,
) -> dict:
    """Assemble one ``CalibrationFixture``-shaped dict from a live capture.

    The ground truth is seeded from the matched ``defect`` label, falling
    back to the captured verdict so the fixture still loads when an extra
    finding has no label. Either way ``notes`` flags it for human review —
    the harness records what happened; the oracle is still hand-confirmed.
    """
    captured_verdict = str(getattr(result, "verdict", "") or "").strip().upper()
    fallback_verdict = captured_verdict if captured_verdict in _VALID_VERDICTS else "UNVERIFIED"
    if defect is not None:
        correct_verdict = defect.expected_verdict
        expected_status = defect.expected_status
        note = (
            f"Auto-captured for labeled defect: {defect.label}. "
            "Confirm correct_verdict / expected_status before trusting this fixture."
        )
    else:
        correct_verdict = fallback_verdict
        expected_status = None
        note = (
            "Auto-captured for an unlabeled finding; correct_verdict seeded "
            "from the captured verdict — confirm by hand."
        )
    return {
        "fixture_id": f"live_{spec.spec_id}_{index}",
        "category": spec.category,
        "severity": str(getattr(finding, "severity", "") or "GRIPES"),
        "description": f"Live capture from {spec.filename}",
        "finding": _finding_payload(finding),
        "spec_context": {
            "filename": spec.filename,
            "cycle_label": cycle_label,
            "paragraph_map_slice": [],
        },
        "captured_verifier_response": _verifier_payload(result),
        "ground_truth": {
            "correct_verdict": correct_verdict,
            "expected_status": expected_status,
            "notes": note,
        },
    }


# ---------------------------------------------------------------------------
# Aggregate review summary (pure).
# ---------------------------------------------------------------------------


@dataclass
class CaptureSummary:
    """Roll-up of one capture run."""

    spec_scores: list[SpecReviewScore] = field(default_factory=list)
    fixtures_written: int = 0

    @property
    def expected_defects(self) -> int:
        return sum(s.expected_defect_count for s in self.spec_scores)

    @property
    def matched_defects(self) -> int:
        return sum(s.matched_defect_count for s in self.spec_scores)

    @property
    def false_positives(self) -> int:
        return sum(s.false_positive_count for s in self.spec_scores)

    @property
    def severity_matches(self) -> int:
        return sum(s.severity_match_count for s in self.spec_scores)

    @property
    def judged_specs(self) -> int:
        return sum(1 for s in self.spec_scores if s.match_method == "judge")

    @property
    def extra_findings(self) -> int:
        return sum(s.extra_finding_count for s in self.spec_scores)

    @property
    def fp_legitimate(self) -> int:
        return sum(s.fp_legitimate for s in self.spec_scores)

    @property
    def fp_duplicates(self) -> int:
        return sum(s.fp_duplicate for s in self.spec_scores)

    @property
    def fp_hallucinations(self) -> int:
        return sum(s.fp_hallucination for s in self.spec_scores)

    @property
    def recall(self) -> float:
        denom = self.expected_defects
        return round(self.matched_defects / denom, 4) if denom else 0.0

    def render(self) -> str:
        total = len(self.spec_scores)
        lines = [
            "Spec Critic live-capture summary",
            "=" * 56,
            f"Specs reviewed:        {total}",
            f"Review recall:         {self.recall:.4f} "
            f"({self.matched_defects}/{self.expected_defects})",
            f"Matcher:               judge on {self.judged_specs}/{total} specs"
            + (
                f" (substring fallback on {total - self.judged_specs})"
                if 0 < self.judged_specs < total
                else ""
            ),
            f"False positives:       {self.false_positives} (clean specs)",
            f"Severity matches:      {self.severity_matches}/{self.matched_defects}",
            f"Calibration fixtures:  {self.fixtures_written}",
        ]
        if self.extra_findings:
            classified = self.fp_legitimate + self.fp_duplicates + self.fp_hallucinations
            lines.append(
                f"Extra findings:        {self.extra_findings} "
                f"(legit {self.fp_legitimate}, dup {self.fp_duplicates}, "
                f"halluc {self.fp_hallucinations}, "
                f"unclassified {self.extra_findings - classified})"
            )
        lines.extend(["", "Per-spec:"])
        for s in self.spec_scores:
            tag = "clean" if s.is_clean else f"{s.matched_defect_count}/{s.expected_defect_count} defects"
            extra = f", {s.false_positive_count} FP" if s.is_clean else ""
            lines.append(
                f"  [{s.spec_id}] {tag}{extra} "
                f"({s.finding_count} findings, via {s.match_method})"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Live orchestration (network — only reached on the --live path).
# ---------------------------------------------------------------------------


def _run_review(spec: LabeledSpec, *, model: str, cycle: Any) -> list[Any]:
    """Call the real review prompt for one spec; return parsed findings.

    Reuses the production request builder and parser so the captured
    behavior is exactly what the pipeline would see. The tool path is the
    common case; a plain-text response yields zero findings here (the
    capture set is small enough to re-run if that happens).
    """
    from src.review import reviewer
    from src.review.review_request_builder import ReviewRequestSpec, build_review_request
    from src.review.structured_schemas import REVIEW_TOOL_NAME, extract_tool_use_block

    built = build_review_request(
        ReviewRequestSpec(
            spec_content=spec.spec_text,
            filename=spec.filename,
            model=model,
            cycle=cycle,
            include_service_tier=False,
        )
    )
    client = reviewer._get_client()
    with client.messages.stream(**built.params) as stream:
        for _ in stream.text_stream:
            pass
        resp = stream.get_final_message()
    payload = extract_tool_use_block(resp, REVIEW_TOOL_NAME) or {}
    raw_findings = payload.get("findings") or []
    return reviewer._parse_findings(raw_findings)


def _judged_matcher(
    spec: LabeledSpec,
    findings: list[Any],
    *,
    judge_model: str | None,
    log: LogFn,
) -> tuple[Any, str]:
    """Resolve the matcher for one spec: judge when usable, else substring.

    Returns ``(matcher, match_method)``. Every judge decision's one-sentence
    reasoning is logged so the run transcript doubles as the audit trail.
    """
    from . import judge

    matches = judge.judge_defect_matches(spec, findings, model=judge_model, log=log)
    if matches is None:
        log(
            f"  judge unavailable for {spec.spec_id}; using substring matcher.",
            level="warning",
        )
        return defect_matched, "substring"
    for m in sorted(matches.values(), key=lambda j: j.defect_index):
        target = "no finding" if m.finding_index is None else f"finding {m.finding_index}"
        reason = f" — {m.reasoning}" if m.reasoning else ""
        log(f"  judge: defect {m.defect_index} -> {target}{reason}", level="info")
    return judge.matcher_from_matches(spec, matches, findings), "judge"


def _classify_extras(
    spec: LabeledSpec,
    findings: list[Any],
    matcher: Any,
    score: Any,
    *,
    judge_model: str | None,
    log: LogFn,
) -> None:
    """Judge-classify findings matched to no defect; fill score telemetry."""
    from . import judge

    # (defect_index, matched finding_index | None) — handed to the judge as
    # reference context so duplicate_of_matched is actually decidable.
    index_by_id = {id(f): i for i, f in enumerate(findings)}
    matched_pairs: list[tuple[int, int | None]] = []
    matched_ids: set[int] = set()
    for defect_idx, defect in enumerate(spec.expected_defects):
        hit = matcher(defect, findings)
        if hit is None:
            matched_pairs.append((defect_idx, None))
            continue
        matched_pairs.append((defect_idx, index_by_id.get(id(hit))))
        matched_ids.add(id(hit))
    extra_indices = [i for i, f in enumerate(findings) if id(f) not in matched_ids]
    score.extra_finding_count = len(extra_indices)
    if not extra_indices:
        return
    classifications = judge.classify_extra_findings(
        spec,
        findings,
        extra_indices,
        matched_pairs=matched_pairs,
        model=judge_model,
        log=log,
    )
    if not classifications:
        return
    for c in sorted(classifications.values(), key=lambda x: x.finding_index):
        if c.classification == "legitimate_unlabeled":
            score.fp_legitimate += 1
        elif c.classification == "duplicate_of_matched":
            score.fp_duplicate += 1
        elif c.classification == "hallucination":
            score.fp_hallucination += 1
        reason = f" — {c.reasoning}" if c.reasoning else ""
        log(
            f"  judge: extra finding {c.finding_index} = {c.classification}{reason}",
            level="info",
        )


def capture(
    *,
    out_dir: Path,
    model: str | None = None,
    judge_enabled: bool = True,
    judge_model: str | None = None,
    log: LogFn = lambda *_a, **_k: None,
) -> CaptureSummary:
    """Run the full live capture over the labeled set. Requires a real key."""
    from src.core.api_config import REVIEW_MODEL_DEFAULT
    from src.core.code_cycles import CALIFORNIA_2025
    from src.verification.verifier import verify_finding

    review_model = model or REVIEW_MODEL_DEFAULT
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = CaptureSummary()

    for spec in LABELED_SPECS:
        log(f"Reviewing {spec.spec_id} ...", level="info")
        findings = _run_review(spec, model=review_model, cycle=CALIFORNIA_2025)

        if judge_enabled:
            matcher, match_method = _judged_matcher(
                spec, findings, judge_model=judge_model, log=log
            )
        else:
            matcher, match_method = defect_matched, "substring"
        score = score_spec_review(spec, findings, matcher=matcher)
        score.match_method = match_method
        if judge_enabled:
            _classify_extras(
                spec, findings, matcher, score, judge_model=judge_model, log=log
            )
        summary.spec_scores.append(score)

        for idx, defect in enumerate(spec.expected_defects):
            hit = matcher(defect, findings)
            if hit is None:
                log(f"  MISS: {defect.label}", level="warning")
                continue
            # Fresh verification (cache=None) so the reframed prompt's effect
            # is measured, not a replayed cached verdict.
            result = verify_finding(hit, cycle=CALIFORNIA_2025, cache=None)
            fixture = build_fixture_dict(
                spec, hit, result, defect,
                cycle_label=CALIFORNIA_2025.label, index=idx,
            )
            path = out_dir / f"{fixture['fixture_id']}.json"
            path.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
            summary.fixtures_written += 1
            log(f"  wrote {path.name} (verdict={result.verdict})", level="info")

    return summary


def _log(msg: str, *, level: str = "info") -> None:
    print(f"[{level}] {msg}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.live_capture",
        description="Run the real review + verification prompts over the labeled "
        "spec set and emit calibration fixtures for the existing scorers.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually call the Anthropic API. Without this flag the command "
        "is a no-op (hermetic default).",
    )
    parser.add_argument(
        "--out-dir",
        default=str(_default_out_dir()),
        help="Directory to write captured fixtures into "
        "(default: evals/calibration/fixtures_live).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the review model (defaults to REVIEW_MODEL_DEFAULT).",
    )
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="Disable the LLM-as-judge matcher and use pure substring "
        "matching (the pre-judge behavior).",
    )
    parser.add_argument(
        "--judge-model",
        default=None,
        help="Override the judge model (defaults to "
        "SPEC_CRITIC_EVAL_JUDGE_MODEL or Haiku 4.5).",
    )
    args = parser.parse_args(argv)

    if not args.live:
        print(
            "Hermetic no-op: pass --live with a real ANTHROPIC_API_KEY to "
            "capture fixtures. Nothing was called.",
            file=sys.stderr,
        )
        return 0
    if not real_key_present():
        print(
            "Refusing to run --live: ANTHROPIC_API_KEY is missing or is the "
            "test sentinel. Set a real key first.",
            file=sys.stderr,
        )
        return 2

    summary = capture(
        out_dir=Path(args.out_dir),
        model=args.model,
        judge_enabled=not args.no_judge,
        judge_model=args.judge_model,
        log=_log,
    )
    print(summary.render())
    print(
        f"\nScore the captured fixtures with:\n"
        f"  python -m evals.calibration.runner --fixtures-dir {args.out_dir}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
