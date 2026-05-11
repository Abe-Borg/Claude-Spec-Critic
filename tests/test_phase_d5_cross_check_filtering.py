"""Phase D5 audit tests for cross-check disputed-upstream filtering.

These tests intentionally document the current tactical policy rather than
introducing a broader dependency rewrite: preserve cross-check findings unless
every cited upstream dependency is disputed and the finding has no independent
raw-evidence ids.
"""

from __future__ import annotations

from src.pipeline import classify_cross_check_dependencies
from src.reviewer import Finding
from src.verifier import VerificationResult


def _review_finding(*, finding_id: str, verdict: str, file: str = "A.docx") -> Finding:
    finding = Finding(
        severity="HIGH",
        fileName=file,
        section="2.1",
        issue=f"Review finding {finding_id}",
        actionType="EDIT",
        existingText="old",
        replacementText="new",
        codeReference="CBC §1234",
        confidence=0.8,
    )
    finding.finding_id = finding_id
    finding.verification = VerificationResult(verdict=verdict, explanation="test")
    return finding


def _cross_finding(
    *,
    upstream_ids: list[str] | None = None,
    independent_ids: list[str] | None = None,
) -> Finding:
    return Finding(
        severity="HIGH",
        fileName="A.docx",
        section="2.1",
        issue="Cross-check coordination finding",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference=None,
        confidence=0.7,
        upstream_finding_ids=list(upstream_ids or []),
        independent_evidence_ids=list(independent_ids or []),
    )


def test_d5_one_disputed_upstream_side_is_preserved() -> None:
    """A partial upstream dispute is not enough to suppress the finding."""
    disputed = _review_finding(finding_id="rf-disputed", verdict="DISPUTED")
    confirmed = _review_finding(finding_id="rf-confirmed", verdict="CONFIRMED", file="B.docx")
    cross = _cross_finding(upstream_ids=["rf-disputed", "rf-confirmed"])

    kept, suppressed = classify_cross_check_dependencies([cross], [disputed, confirmed])

    assert kept == [cross]
    assert suppressed == []
    assert cross.suppression_reason is None


def test_d5_both_upstream_sides_disputed_are_suppressed_without_raw_evidence() -> None:
    """All cited upstream dependencies disputed + no raw evidence is suppressed."""
    first = _review_finding(finding_id="rf-a", verdict="DISPUTED")
    second = _review_finding(finding_id="rf-b", verdict="DISPUTED", file="B.docx")
    cross = _cross_finding(upstream_ids=["rf-a", "rf-b"])

    kept, suppressed = classify_cross_check_dependencies([cross], [first, second])

    assert kept == []
    assert suppressed == [cross]
    assert "rf-a" in (cross.suppression_reason or "")
    assert "rf-b" in (cross.suppression_reason or "")


def test_d5_no_upstream_sides_disputed_are_preserved() -> None:
    """When cited upstream findings still stand, cross-check output stays."""
    first = _review_finding(finding_id="rf-a", verdict="CONFIRMED")
    second = _review_finding(finding_id="rf-b", verdict="UNVERIFIED", file="B.docx")
    cross = _cross_finding(upstream_ids=["rf-a", "rf-b"])

    kept, suppressed = classify_cross_check_dependencies([cross], [first, second])

    assert kept == [cross]
    assert suppressed == []


def test_d5_raw_evidence_preserves_cross_check_finding_when_all_upstream_disputed() -> None:
    """Independent paragraph/cell evidence prevents over-dropping."""
    first = _review_finding(finding_id="rf-a", verdict="DISPUTED")
    second = _review_finding(finding_id="rf-b", verdict="DISPUTED", file="B.docx")
    cross = _cross_finding(
        upstream_ids=["rf-a", "rf-b"],
        independent_ids=["A.docx:p12", "B.docx:t3r2"],
    )

    kept, suppressed = classify_cross_check_dependencies([cross], [first, second])

    assert kept == [cross]
    assert suppressed == []
    assert cross.suppression_reason is None
