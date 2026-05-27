"""Harness logic for the golden-set eval suite.

This module exposes one entrypoint, :func:`run_harness`, that walks the
fixture taxonomy from :mod:`evals.fixtures`, exercises the relevant
production code paths offline, and collates the per-fixture and aggregate
metric values into a JSON-friendly result dict.

Metric coverage (one row per Chunk 12 plan bullet):

* ``review_recall``                — found / seeded review findings.
* ``false_positive_count``         — model findings on clean specs.
* ``duplicate_finding_rate``       — duplicate fileName+section+issue triples.
* ``parse_failure_rate``           — fixture findings dropped by the parser.
* ``edit_proposal_validity``       — surviving EDIT proposals over expected.
* ``citation_acceptance_rate``     — accepted / total cited URLs.
* ``sourceless_confirmed_rate``    — CONFIRMED that survived without a citation.

Each metric is reported as a numerator/denominator pair (counts) so the
runner can render rate and absolute numbers without losing the underlying
volume. The aggregate ``metrics`` dict carries derived ``rate`` floats for
quick diffing against the baseline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.output import report_status
from src.verification import source_grounding, verifier
from src.input import preprocessor
from src.review import reviewer
from src.core.api_config import MODEL_SONNET_46
from src.core.code_cycles import CALIFORNIA_2025

from .fixtures import (
    GoldenFixture,
    all_fixtures,
)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FixtureResult:
    """Per-fixture outcomes (no aggregation — counts only)."""

    fixture_id: str
    category: str
    description: str
    review_findings_parsed: int = 0
    review_findings_expected: int = 0
    seeded_finding_count: int = 0
    duplicate_finding_count: int = 0
    parse_failure_count: int = 0
    # Input findings whose raw ``actionType`` was an executable edit
    # (EDIT/ADD/DELETE). The Chunk 7 contract requires that every one of
    # these either parses into a valid :class:`EditProposal` or gets
    # demoted with a ``demotion_reason``. The denominator of the
    # edit-proposal-validity metric.
    edit_proposal_input_count: int = 0
    # Parsed findings whose ``as_edit_proposal()`` returns non-None — i.e.
    # the parser preserved them as executable edits. Numerator of the
    # edit-proposal-validity metric.
    edit_proposal_valid_count: int = 0
    demoted_findings: int = 0
    verification_initial_verdict: str = ""
    verification_final_verdict: str = ""
    cited_citation_count: int = 0
    accepted_citation_count: int = 0
    downgrade_observed: bool = False
    downgrade_expected: bool = False
    preprocessor_alert_count: int = 0
    preprocessor_rules_seen: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Fixture passes when no per-fixture issue was recorded."""
        return not self.issues


@dataclass
class HarnessResult:
    """Aggregate output of :func:`run_harness`."""

    fixtures: list[FixtureResult] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Per-fixture runners
# ---------------------------------------------------------------------------


def _safe_divide(numer: float, denom: float) -> float:
    if denom <= 0:
        return 0.0
    return round(numer / denom, 4)


def _is_duplicate_finding(seen: set[tuple[str, str, str]], f) -> bool:
    """Return True if (fileName, section, issue) was already observed."""
    key = ((f.fileName or "").strip(), (f.section or "").strip(), (f.issue or "").strip())
    if key in seen:
        return True
    seen.add(key)
    return False


def _run_review_fixture(fixture: GoldenFixture, fr: FixtureResult) -> None:
    """Parse a fixture's review payload through ``src.reviewer._parse_findings``.

    Tracks recall (model emitted N findings; parser kept M), parse failures
    (payload had a finding entry that the parser silently dropped),
    duplicate rate, and Chunk 7 demotion behavior.
    """
    payload = fixture.review_payload
    if payload is None:
        return

    raw_findings = list(payload.get("findings") or [])
    fr.seeded_finding_count = int(fixture.expected.get("seeded_finding_count", len(raw_findings)))
    fr.review_findings_expected = int(
        fixture.expected.get("expected_review_findings", len(raw_findings))
    )

    parsed = reviewer._parse_findings(raw_findings)
    fr.review_findings_parsed = len(parsed)
    fr.parse_failure_count = max(0, len(raw_findings) - len(parsed))

    # The denominator of the edit-proposal-validity metric is the input
    # count of executable-edit findings — anything the model emitted as
    # ``EDIT`` / ``ADD`` / ``DELETE``. Counting on the *raw* payload (rather
    # than ``parsed``) catches the case where the parser drops a finding
    # outright (e.g., missing ``issue``) so a regression there shows up.
    fr.edit_proposal_input_count = sum(
        1
        for item in raw_findings
        if isinstance(item, dict)
        and str(item.get("actionType") or "").strip().upper()
        in {"EDIT", "ADD", "DELETE"}
    )

    seen: set[tuple[str, str, str]] = set()
    valid_edit_proposals = 0
    demoted = 0
    for f in parsed:
        if _is_duplicate_finding(seen, f):
            fr.duplicate_finding_count += 1
        if f.as_edit_proposal() is not None:
            valid_edit_proposals += 1
        if (f.demotion_reason or "").strip():
            demoted += 1
    fr.edit_proposal_valid_count = valid_edit_proposals
    fr.demoted_findings = demoted
    expected_demoted = fixture.expected.get("expected_demoted_findings")
    if expected_demoted is not None and demoted != int(expected_demoted):
        fr.issues.append(
            f"demoted findings: expected {expected_demoted}, got {demoted}"
        )
    expected_valid = fixture.expected.get("expected_edit_proposal_valid")
    if expected_valid is not None and valid_edit_proposals != int(expected_valid):
        fr.issues.append(
            f"valid edit proposals: expected {expected_valid}, "
            f"got {valid_edit_proposals}"
        )

    expected_review = fixture.expected.get("expected_review_findings")
    if expected_review is not None and fr.review_findings_parsed != int(expected_review):
        fr.issues.append(
            f"parsed review findings: expected {expected_review}, "
            f"got {fr.review_findings_parsed}"
        )
    expected_report_only = fixture.expected.get("expected_report_only")
    if expected_report_only is not None:
        actual_report_only = sum(
            1 for f in parsed if (f.actionType or "").upper() == "REPORT_ONLY"
        )
        if actual_report_only != int(expected_report_only):
            fr.issues.append(
                f"report-only count: expected {expected_report_only}, "
                f"got {actual_report_only}"
            )


def _run_preprocessor_fixture(fixture: GoldenFixture, fr: FixtureResult) -> None:
    """Run the deterministic detectors against the fixture's spec text."""
    if not fixture.spec_text:
        return
    result = preprocessor.preprocess_spec(
        fixture.spec_text, fixture.filename, cycle=CALIFORNIA_2025
    )
    rules: list[str] = []
    for bucket in (
        result.leed_alerts,
        result.placeholder_alerts,
        result.code_cycle_alerts,
        result.structural_alerts,
        result.template_marker_alerts,
        result.invalid_code_cycle_alerts,
        result.duplicate_paragraph_alerts,
    ):
        for alert in bucket:
            rule = alert.get("deterministic_rule", "")
            if rule:
                rules.append(rule)
    fr.preprocessor_alert_count = len(rules)
    fr.preprocessor_rules_seen = sorted(set(rules))

    expected_zero = fixture.expected.get("preprocessor_alerts_expected")
    if expected_zero is not None and fr.preprocessor_alert_count != int(expected_zero):
        fr.issues.append(
            f"preprocessor alerts: expected {expected_zero}, "
            f"got {fr.preprocessor_alert_count}"
        )
    expected_min = fixture.expected.get("preprocessor_alerts_expected_min")
    if expected_min is not None and fr.preprocessor_alert_count < int(expected_min):
        fr.issues.append(
            f"preprocessor alerts below expected minimum: expected ≥{expected_min}, "
            f"got {fr.preprocessor_alert_count}"
        )
    expected_rule = fixture.expected.get("preprocessor_rule_expected")
    if expected_rule is not None and expected_rule not in fr.preprocessor_rules_seen:
        fr.issues.append(
            f"missing expected preprocessor rule: {expected_rule}"
        )
    expected_any = fixture.expected.get("preprocessor_rules_expected_any")
    if expected_any:
        if not any(r in fr.preprocessor_rules_seen for r in expected_any):
            fr.issues.append(
                f"none of expected preprocessor rules present: {sorted(expected_any)}"
            )


def _run_verification_fixture(fixture: GoldenFixture, fr: FixtureResult) -> None:
    """Apply source-grounding + invariant enforcement to a verdict payload."""
    payload = fixture.verification_payload
    if payload is None:
        return

    initial_verdict = str(payload.get("verdict") or "").upper()
    fr.verification_initial_verdict = initial_verdict
    result = verifier.VerificationResult(
        verdict=initial_verdict,
        explanation=str(payload.get("explanation") or ""),
        sources=list(payload.get("sources") or []),
        grounded=bool(fixture.searched_urls),
        model_used=MODEL_SONNET_46,
    )
    searched = [
        source_grounding.SearchedSource(url=u, title="")
        for u in fixture.searched_urls
    ]
    result = verifier._apply_source_grounding(result, searched=searched)
    result = verifier._enforce_grounding_invariant(result)

    fr.verification_final_verdict = (result.verdict or "").upper()
    fr.cited_citation_count = len(result.cited_sources)
    fr.accepted_citation_count = len(result.accepted_sources)
    fr.downgrade_observed = (
        initial_verdict in ("CONFIRMED", "CORRECTED")
        and fr.verification_final_verdict not in ("CONFIRMED", "CORRECTED")
    )
    fr.downgrade_expected = bool(fixture.expected.get("expected_downgrade", False))

    expected_final = fixture.expected.get("expected_verdict_after_grounding")
    if expected_final is not None and fr.verification_final_verdict != str(expected_final).upper():
        fr.issues.append(
            f"final verdict: expected {expected_final}, "
            f"got {fr.verification_final_verdict}"
        )
    expected_accepted = fixture.expected.get("expected_accepted_citation_count")
    if expected_accepted is not None and fr.accepted_citation_count != int(expected_accepted):
        fr.issues.append(
            f"accepted citations: expected {expected_accepted}, "
            f"got {fr.accepted_citation_count}"
        )
    if fr.downgrade_observed != fr.downgrade_expected:
        fr.issues.append(
            f"downgrade observed={fr.downgrade_observed}, expected={fr.downgrade_expected}"
        )

    # Sanity-check report-status classifier — confirms Chunk 5's belt-and-
    # suspenders accepted-citation check on supportive statuses lines up
    # with the source-grounding outcome.
    finding = reviewer.Finding(
        severity="HIGH",
        fileName=fixture.filename or "verification.docx",
        section="1.01",
        issue="Verification harness probe",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference=None,
        verification=result,
    )
    status = report_status.classify_status(finding)
    # If the harness downgraded, the supportive status branch should not
    # apply — guard a regression where a future helper bypasses the
    # invariant and classify_status mis-promotes.
    if fr.downgrade_observed and status in (
        report_status.ReportStatus.VERIFIED_SUPPORTED,
        report_status.ReportStatus.VERIFIED_CONTRADICTED,
    ):
        fr.issues.append(
            f"downgraded verdict mis-classified to supportive status: {status.value}"
        )


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def run_harness() -> HarnessResult:
    """Run every fixture and return aggregate metrics."""
    fixture_results: list[FixtureResult] = []
    for fixture in all_fixtures():
        fr = FixtureResult(
            fixture_id=fixture.fixture_id,
            category=fixture.category,
            description=fixture.description,
        )
        _run_review_fixture(fixture, fr)
        _run_preprocessor_fixture(fixture, fr)
        _run_verification_fixture(fixture, fr)
        fixture_results.append(fr)

    metrics = _aggregate_metrics(fixture_results)
    return HarnessResult(fixtures=fixture_results, metrics=metrics)


# ---------------------------------------------------------------------------
# Metric aggregation
# ---------------------------------------------------------------------------


def _aggregate_metrics(results: list[FixtureResult]) -> dict[str, Any]:
    """Roll per-fixture counters into the golden-set metrics."""
    seeded_total = sum(r.seeded_finding_count for r in results)
    parsed_total = sum(r.review_findings_parsed for r in results)

    duplicate_total = sum(r.duplicate_finding_count for r in results)
    parse_failures = sum(r.parse_failure_count for r in results)

    # Recall counts findings the parser surfaced relative to what the
    # fixture seeded — capped at the seeded count so an over-emit
    # cannot make recall exceed 1.0.
    recall_numer = sum(
        min(r.review_findings_parsed, r.seeded_finding_count) for r in results
    )

    false_positive_count = sum(
        r.review_findings_parsed
        for r in results
        if r.category == "clean_spec"
    )

    edit_valid_total = sum(r.edit_proposal_valid_count for r in results)
    edit_input_total = sum(r.edit_proposal_input_count for r in results)

    cited_total = sum(r.cited_citation_count for r in results)
    accepted_total = sum(r.accepted_citation_count for r in results)

    confirmed_initial = sum(
        1
        for r in results
        if r.verification_initial_verdict in ("CONFIRMED", "CORRECTED")
    )
    sourceless_confirmed_survivors = sum(
        1
        for r in results
        if r.verification_initial_verdict in ("CONFIRMED", "CORRECTED")
        and r.verification_final_verdict in ("CONFIRMED", "CORRECTED")
        and r.accepted_citation_count == 0
    )

    fixture_pass_count = sum(1 for r in results if r.passed)

    return {
        "fixture_count": len(results),
        "fixture_pass_count": fixture_pass_count,
        "fixture_fail_count": len(results) - fixture_pass_count,
        "review_recall": {
            "numerator": recall_numer,
            "denominator": seeded_total,
            "rate": _safe_divide(recall_numer, seeded_total),
        },
        "false_positive_count": false_positive_count,
        "duplicate_finding": {
            "numerator": duplicate_total,
            "denominator": parsed_total,
            "rate": _safe_divide(duplicate_total, parsed_total),
        },
        "parse_failure": {
            "numerator": parse_failures,
            "denominator": seeded_total,
            "rate": _safe_divide(parse_failures, seeded_total),
        },
        "edit_proposal_validity": {
            "numerator": edit_valid_total,
            "denominator": edit_input_total,
            "rate": _safe_divide(edit_valid_total, edit_input_total),
        },
        "citation_acceptance": {
            "numerator": accepted_total,
            "denominator": cited_total,
            "rate": _safe_divide(accepted_total, cited_total),
        },
        "sourceless_confirmed": {
            "numerator": sourceless_confirmed_survivors,
            "denominator": confirmed_initial,
            "rate": _safe_divide(sourceless_confirmed_survivors, confirmed_initial),
        },
    }
