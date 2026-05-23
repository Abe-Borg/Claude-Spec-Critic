"""Calibration scorer — convert fixture outcomes into trust metrics.

Five tables, each defined precisely so a future Chunk-1 tuning pass can
read the same numbers across runs:

1. **Confusion matrix.** Rows = ``ground_truth.correct_verdict``;
   columns = the verdict the pipeline emitted *after* grounding (so an
   ungrounded CONFIRMED that downgraded to UNVERIFIED counts as
   UNVERIFIED, not CONFIRMED). Includes a per-row recall column and a
   per-column precision column so the report shows both directions of
   error.

2. **Per-status accuracy.** For every :class:`ReportStatus` the
   classifier assigned, how often did the fixture's
   ``ground_truth.expected_status`` agree? Fixtures with no
   ``expected_status`` are excluded from the denominator.

3. **False-positive auto-edit rate** at four ``edit_confidence``
   thresholds (0.70 / 0.80 / 0.85 / 0.90). At each threshold we count
   fixtures that the pipeline would treat as ``AUTO_EDIT_CANDIDATE``
   (supportive status + ``edit_confidence >= threshold``) and split
   them into "would have edited the right thing" vs. "would have
   edited the wrong thing" using the ground-truth verdict.

4. **Calibration plot.** Bucket every fixture by
   ``finding.confidence`` (the model's self-reported confidence in the
   underlying claim) into five buckets and report the observed
   correctness rate per bucket. Correctness uses the verdict match —
   "the pipeline emitted the verdict the human labeled as right."

5. **Source-grounding integrity.** Count CONFIRMED / CORRECTED
   verdicts (both the captured pre-grounding verdict and the
   post-grounding verdict) that survived without an accepted citation.
   The post-grounding count should always be zero in production; the
   captured-but-downgraded count tells us how often the grounding
   invariant fires.

The scorer also reports the per-fixture pass/fail block + a header
summary (total fixtures, verdict-correct count, recall, fixture pass
count) so a human glancing at the report can answer "did anything
break?" without scrolling.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .harness import FixtureOutcome, HarnessResult


# Verdict columns / rows used in the confusion matrix. Order is stable
# (CONFIRMED → CORRECTED → DISPUTED → UNVERIFIED) so the rendered table
# always reads the same way.
_VERDICTS: tuple[str, ...] = ("CONFIRMED", "CORRECTED", "DISPUTED", "UNVERIFIED")

# AUTO_EDIT confidence thresholds called out in the Chunk 1 plan.
_EDIT_CONFIDENCE_THRESHOLDS: tuple[float, ...] = (0.70, 0.80, 0.85, 0.90)

# Calibration plot buckets. The lower bound is inclusive, the upper bound
# exclusive (except for the last bucket which is inclusive on both ends).
_CONFIDENCE_BUCKETS: tuple[tuple[float, float, str], ...] = (
    (0.0, 0.50, "0.00 – 0.49"),
    (0.50, 0.70, "0.50 – 0.69"),
    (0.70, 0.80, "0.70 – 0.79"),
    (0.80, 0.90, "0.80 – 0.89"),
    (0.90, 1.0001, "0.90 – 1.00"),
)


# ---------------------------------------------------------------------------
# Metric dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ConfusionRow:
    """One row of the confusion matrix.

    ``counts`` is keyed by predicted verdict (one of ``_VERDICTS``).
    ``recall`` is the row's correct-cell count over the row total.
    """

    expected: str
    counts: dict[str, int] = field(default_factory=dict)
    row_total: int = 0
    correct: int = 0

    @property
    def recall(self) -> float:
        return _safe_div(self.correct, self.row_total)


@dataclass
class ConfusionMatrix:
    rows: list[ConfusionRow] = field(default_factory=list)
    column_totals: dict[str, int] = field(default_factory=dict)
    column_correct: dict[str, int] = field(default_factory=dict)

    def precision(self, verdict: str) -> float:
        return _safe_div(
            self.column_correct.get(verdict, 0),
            self.column_totals.get(verdict, 0),
        )


@dataclass
class StatusAccuracy:
    status: str
    assigned: int = 0
    correct: int = 0
    expected_count: int = 0  # fixtures that asked for this status

    @property
    def precision(self) -> float:
        return _safe_div(self.correct, self.assigned)

    @property
    def recall(self) -> float:
        return _safe_div(self.correct, self.expected_count)


@dataclass
class AutoEditFalsePositive:
    threshold: float
    auto_edit_count: int = 0
    correct_count: int = 0
    incorrect_count: int = 0

    @property
    def false_positive_rate(self) -> float:
        return _safe_div(self.incorrect_count, self.auto_edit_count)


@dataclass
class CalibrationBucket:
    label: str
    lower: float
    upper: float
    n: int = 0
    correct: int = 0

    @property
    def correctness_rate(self) -> float:
        return _safe_div(self.correct, self.n)


@dataclass
class GroundingIntegrity:
    """Source-grounding integrity counts."""

    captured_supportive_total: int = 0
    captured_supportive_with_accepted: int = 0
    captured_supportive_without_accepted: int = 0
    grounded_supportive_total: int = 0
    grounded_supportive_with_accepted: int = 0
    grounded_supportive_without_accepted: int = 0
    downgrades_from_captured: int = 0  # captured supportive → final not supportive


@dataclass
class CalibrationReport:
    total_fixtures: int
    verdict_correct: int
    fixture_pass: int
    fixture_fail: int
    confusion_matrix: ConfusionMatrix
    status_accuracy: list[StatusAccuracy]
    auto_edit_fp: list[AutoEditFalsePositive]
    calibration: list[CalibrationBucket]
    grounding_integrity: GroundingIntegrity
    outcomes: list[FixtureOutcome]

    @property
    def overall_verdict_accuracy(self) -> float:
        return _safe_div(self.verdict_correct, self.total_fixtures)


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------


def _safe_div(numer: int | float, denom: int | float) -> float:
    if denom <= 0:
        return 0.0
    return round(float(numer) / float(denom), 4)


def _build_confusion_matrix(outcomes: Iterable[FixtureOutcome]) -> ConfusionMatrix:
    rows = {
        v: ConfusionRow(expected=v, counts={col: 0 for col in _VERDICTS})
        for v in _VERDICTS
    }
    column_totals = {v: 0 for v in _VERDICTS}
    column_correct = {v: 0 for v in _VERDICTS}
    for o in outcomes:
        expected = o.expected_verdict if o.expected_verdict in _VERDICTS else "UNVERIFIED"
        predicted = (
            o.grounded_verdict if o.grounded_verdict in _VERDICTS else "UNVERIFIED"
        )
        row = rows[expected]
        row.counts[predicted] = row.counts.get(predicted, 0) + 1
        row.row_total += 1
        column_totals[predicted] += 1
        if expected == predicted:
            row.correct += 1
            column_correct[predicted] += 1
    return ConfusionMatrix(
        rows=[rows[v] for v in _VERDICTS],
        column_totals=column_totals,
        column_correct=column_correct,
    )


def _build_status_accuracy(outcomes: Iterable[FixtureOutcome]) -> list[StatusAccuracy]:
    by_status: dict[str, StatusAccuracy] = {}
    for o in outcomes:
        actual = by_status.setdefault(o.actual_status, StatusAccuracy(status=o.actual_status))
        actual.assigned += 1
        if o.expected_status is None:
            continue
        if o.status_match:
            actual.correct += 1
        if o.expected_status:
            row = by_status.setdefault(
                o.expected_status, StatusAccuracy(status=o.expected_status)
            )
            row.expected_count += 1
    return sorted(by_status.values(), key=lambda r: r.status)


def _build_auto_edit_fp(
    outcomes: Iterable[FixtureOutcome],
) -> list[AutoEditFalsePositive]:
    """Count AUTO_EDIT findings that the ground truth says were wrong.

    The eligibility rule mirrors :func:`classify_edit_action`: a finding
    qualifies for AUTO_EDIT when the pipeline labeled it
    ``AUTO_EDIT_CANDIDATE`` *and* its ``edit_confidence`` reaches the
    threshold. We re-walk the threshold here rather than reading
    ``actual_edit_action`` so a future change to the production floor
    cannot silently lower or raise our reported numbers.
    """
    rows = [AutoEditFalsePositive(threshold=t) for t in _EDIT_CONFIDENCE_THRESHOLDS]
    for o in outcomes:
        # Only findings whose pipeline status is supportive can become
        # AUTO_EDIT candidates. Mirror the production gate:
        # AUTO_EDIT requires actual_status in the supportive set AND
        # edit_confidence >= floor. ``actual_edit_action`` already
        # encodes both conditions at the production floor; for stricter
        # thresholds we re-check the edit confidence.
        if o.edit_confidence is None:
            continue
        if o.actual_status not in ("VERIFIED_SUPPORTED", "VERIFIED_CONTRADICTED", "LOCALLY_CLASSIFIED"):
            continue
        for row in rows:
            if o.edit_confidence >= row.threshold:
                row.auto_edit_count += 1
                if o.verdict_match:
                    row.correct_count += 1
                else:
                    row.incorrect_count += 1
    return rows


def _build_calibration(outcomes: Iterable[FixtureOutcome]) -> list[CalibrationBucket]:
    buckets = [
        CalibrationBucket(label=label, lower=lo, upper=hi)
        for lo, hi, label in _CONFIDENCE_BUCKETS
    ]
    for o in outcomes:
        conf = float(o.finding_confidence)
        for bucket in buckets:
            if bucket.lower <= conf < bucket.upper:
                bucket.n += 1
                if o.verdict_match:
                    bucket.correct += 1
                break
    return buckets


def _build_grounding_integrity(
    outcomes: Iterable[FixtureOutcome],
) -> GroundingIntegrity:
    integrity = GroundingIntegrity()
    supportive_verdicts = {"CONFIRMED", "CORRECTED"}
    for o in outcomes:
        captured_supportive = o.captured_verdict in supportive_verdicts
        grounded_supportive = o.grounded_verdict in supportive_verdicts
        if captured_supportive:
            integrity.captured_supportive_total += 1
            if o.accepted_count > 0:
                integrity.captured_supportive_with_accepted += 1
            else:
                integrity.captured_supportive_without_accepted += 1
            if not grounded_supportive:
                integrity.downgrades_from_captured += 1
        if grounded_supportive:
            integrity.grounded_supportive_total += 1
            if o.accepted_count > 0:
                integrity.grounded_supportive_with_accepted += 1
            else:
                integrity.grounded_supportive_without_accepted += 1
    return integrity


def score(result: HarnessResult) -> CalibrationReport:
    """Compute every metric and bundle it into a :class:`CalibrationReport`."""
    outcomes = list(result.outcomes)
    verdict_correct = sum(1 for o in outcomes if o.verdict_match)
    fixture_pass = sum(1 for o in outcomes if not o.issues)
    return CalibrationReport(
        total_fixtures=len(outcomes),
        verdict_correct=verdict_correct,
        fixture_pass=fixture_pass,
        fixture_fail=len(outcomes) - fixture_pass,
        confusion_matrix=_build_confusion_matrix(outcomes),
        status_accuracy=_build_status_accuracy(outcomes),
        auto_edit_fp=_build_auto_edit_fp(outcomes),
        calibration=_build_calibration(outcomes),
        grounding_integrity=_build_grounding_integrity(outcomes),
        outcomes=outcomes,
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _fmt_pct(rate: float) -> str:
    return f"{rate * 100:6.2f}%"


def _fmt_int(n: int, *, width: int = 4) -> str:
    return f"{n:>{width}d}"


def _render_header(report: CalibrationReport) -> str:
    lines = [
        "# Spec Critic Calibration Eval",
        "",
        "_Replay of hand-labeled fixtures through the production grounding +"
        " classification helpers._",
        "",
        "## Summary",
        "",
        f"- **Total fixtures:** {report.total_fixtures}",
        f"- **Verdict accuracy (matched ground truth):** "
        f"{report.verdict_correct} / {report.total_fixtures} "
        f"({_fmt_pct(report.overall_verdict_accuracy)})",
        f"- **Fixture pass / fail (no per-fixture issues):** "
        f"{report.fixture_pass} pass, {report.fixture_fail} fail",
        "",
    ]
    return "\n".join(lines)


def _render_confusion_matrix(matrix: ConfusionMatrix) -> str:
    header_cols = " | ".join(f"pred {v}" for v in _VERDICTS)
    lines = [
        "## 1. Confusion matrix",
        "",
        "Rows are the ground-truth verdict; columns are the verdict the"
        " pipeline emitted after grounding.",
        "",
        f"| expected \\ predicted | {header_cols} | row total | recall |",
        "|---|" + "|".join(["---"] * (len(_VERDICTS) + 2)) + "|",
    ]
    for row in matrix.rows:
        cells = " | ".join(_fmt_int(row.counts.get(v, 0)) for v in _VERDICTS)
        lines.append(
            f"| {row.expected} | {cells} | {_fmt_int(row.row_total)} "
            f"| {_fmt_pct(row.recall)} |"
        )
    # Per-column precision row.
    cells = " | ".join(_fmt_pct(matrix.precision(v)) for v in _VERDICTS)
    lines.append(f"| **precision** | {cells} | — | — |")
    lines.append("")
    return "\n".join(lines)


def _render_status_accuracy(rows: list[StatusAccuracy]) -> str:
    lines = [
        "## 2. Per-status accuracy",
        "",
        "_Precision_ = of findings the pipeline assigned this status, how"
        " many matched the fixture's ``expected_status``.  _Recall_ = of"
        " findings whose fixture expected this status, how many the"
        " pipeline actually produced.",
        "",
        "| status | assigned | expected | correct | precision | recall |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row.status} | {_fmt_int(row.assigned)} "
            f"| {_fmt_int(row.expected_count)} "
            f"| {_fmt_int(row.correct)} "
            f"| {_fmt_pct(row.precision)} | {_fmt_pct(row.recall)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_auto_edit_fp(rows: list[AutoEditFalsePositive]) -> str:
    lines = [
        "## 3. False-positive auto-edit rate",
        "",
        "At each ``edit_confidence`` threshold, count fixtures the pipeline"
        " would auto-edit (supportive status + threshold met) and split"
        " them by whether the ground-truth verdict matched the verdict the"
        " pipeline emitted. A high FP rate is the trust signal Chunk 1 is"
        " here to measure.",
        "",
        "| threshold | auto-edit eligible | correct | incorrect | FP rate |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| ≥ {row.threshold:.2f} | {_fmt_int(row.auto_edit_count)} "
            f"| {_fmt_int(row.correct_count)} "
            f"| {_fmt_int(row.incorrect_count)} "
            f"| {_fmt_pct(row.false_positive_rate)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_calibration(buckets: list[CalibrationBucket]) -> str:
    lines = [
        "## 4. Confidence calibration",
        "",
        "Findings bucketed by ``finding.confidence`` (the model's"
        " self-reported confidence). A well-calibrated model has a"
        " correctness rate close to the midpoint of each bucket.",
        "",
        "| confidence bucket | n | correct | correctness rate |",
        "|---|---|---|---|",
    ]
    for bucket in buckets:
        lines.append(
            f"| {bucket.label} | {_fmt_int(bucket.n)} "
            f"| {_fmt_int(bucket.correct)} "
            f"| {_fmt_pct(bucket.correctness_rate)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_grounding_integrity(integrity: GroundingIntegrity) -> str:
    captured_no_citation_rate = _safe_div(
        integrity.captured_supportive_without_accepted,
        integrity.captured_supportive_total,
    )
    final_no_citation_rate = _safe_div(
        integrity.grounded_supportive_without_accepted,
        integrity.grounded_supportive_total,
    )
    lines = [
        "## 5. Source-grounding integrity",
        "",
        "Captured = before the grounding invariant ran. Final = after"
        " ``_apply_source_grounding`` + ``_enforce_grounding_invariant``."
        " The final-without-accepted row should always be 0 — non-zero"
        " means the invariant has a hole.",
        "",
        "| dimension | total | with accepted citation | without accepted citation | uncited rate |",
        "|---|---|---|---|---|",
        f"| captured CONFIRMED/CORRECTED | {integrity.captured_supportive_total} "
        f"| {integrity.captured_supportive_with_accepted} "
        f"| {integrity.captured_supportive_without_accepted} "
        f"| {_fmt_pct(captured_no_citation_rate)} |",
        f"| final CONFIRMED/CORRECTED | {integrity.grounded_supportive_total} "
        f"| {integrity.grounded_supportive_with_accepted} "
        f"| {integrity.grounded_supportive_without_accepted} "
        f"| {_fmt_pct(final_no_citation_rate)} |",
        "",
        f"- **Captured supportive verdicts downgraded by grounding:** "
        f"{integrity.downgrades_from_captured}",
        "",
    ]
    return "\n".join(lines)


def _render_per_fixture(outcomes: list[FixtureOutcome]) -> str:
    lines = [
        "## Per-fixture detail",
        "",
        "| fixture_id | severity | category | verdict (captured → grounded) "
        "| expected | match | status | edit action |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for o in outcomes:
        match = "✓" if o.verdict_match else "✗"
        lines.append(
            f"| `{o.fixture_id}` | {o.severity} | {o.category} "
            f"| {o.captured_verdict} → {o.grounded_verdict} "
            f"| {o.expected_verdict} | {match} | {o.actual_status} "
            f"| {o.actual_edit_action} |"
        )
    lines.append("")
    fail_block: list[str] = []
    for o in outcomes:
        if not o.issues:
            continue
        fail_block.append(f"### `{o.fixture_id}` issues")
        for issue in o.issues:
            fail_block.append(f"- {issue}")
        fail_block.append("")
    if fail_block:
        lines.append("### Fixture issues")
        lines.append("")
        lines.extend(fail_block)
    return "\n".join(lines)


def render_markdown(report: CalibrationReport) -> str:
    """Compose the full markdown scoring report."""
    sections = [
        _render_header(report),
        _render_confusion_matrix(report.confusion_matrix),
        _render_status_accuracy(report.status_accuracy),
        _render_auto_edit_fp(report.auto_edit_fp),
        _render_calibration(report.calibration),
        _render_grounding_integrity(report.grounding_integrity),
        _render_per_fixture(report.outcomes),
    ]
    return "\n".join(sections)
