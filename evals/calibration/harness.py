"""Calibration harness — replay captured verifier outputs through production
grounding + classification, collect per-fixture outcomes for the scorer.

The harness is intentionally narrow: it does **not** call the verifier
LLM. Each fixture carries a captured verifier response; the harness
reconstructs a :class:`VerificationResult`, runs the same grounding /
invariant helpers the production pipeline uses, attaches the result to a
:class:`Finding`, and asks the production status / edit-action
classifiers what label they would assign. The scorer then compares those
labels to the fixture's hand-labeled ground truth.

Everything the scorer needs flows out via :class:`FixtureOutcome` so the
metrics layer stays decoupled from the production imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from src.core.api_config import (
    DEFAULT_VERIFICATION_MAX_USES,
    web_search_max_uses_for_severity,
)
from src.output import report_status
from src.review.reviewer import Finding
from src.verification import verifier
from src.verification.source_grounding import SearchedSource

from .loader import CalibrationFixture, CapturedVerifierResponse, FindingPayload


# Cache status sentinel for the local-skip path — mirrors
# :func:`src.verification.verifier._local_skip_result`. Re-declared here so
# the harness does not reach into a private constant.
_LOCAL_SKIP = "local_skip"


@dataclass
class FixtureOutcome:
    """Per-fixture result the scorer consumes.

    Fields are deliberately concrete (no enums) so the JSON-rendered
    outcome round-trips cleanly through ``json.dumps``.
    """

    fixture_id: str
    category: str
    severity: str
    description: str

    # The verdict the captured response carried before grounding ran.
    captured_verdict: str
    # The verdict after :func:`_apply_source_grounding` +
    # :func:`_enforce_grounding_invariant`.
    grounded_verdict: str
    # ``correct_verdict`` from the fixture's ground-truth block.
    expected_verdict: str

    # The model's self-reported confidence on the underlying finding —
    # bucketed for the calibration plot. Pulled from ``finding.confidence``.
    finding_confidence: float

    # Trust-model status after grounding.
    actual_status: str

    # Expected status from the fixture (may be None when the fixture only
    # labels the verdict).
    expected_status: str | None

    # Source-grounding evidence after the helpers ran.
    cited_count: int
    accepted_count: int
    grounded: bool
    cache_status: str

    # Whether the fixture's classifier outcomes matched the ground truth.
    # Verdict match is the primary correctness signal; status match is
    # tracked separately when the fixture labels it.
    verdict_match: bool
    status_match: bool | None

    # Captured response metadata used by the scorer.
    verification_mode: str
    verification_profile: str
    web_search_requests: int
    web_search_budget: int

    # Chunk 13 / Trust Upgrade: True when the verifier exhausted its
    # mode-scaled search budget without grounding a verdict. Computed
    # by :func:`_apply_budget_exhaustion` in the harness so a fixture
    # whose captured response had ``web_search_requests`` at or above
    # the severity-tiered budget surfaces the flag through the scorer.
    budget_exhausted: bool = False

    notes: str = ""
    issues: list[str] = field(default_factory=list)


@dataclass
class HarnessResult:
    """Aggregate harness output handed to the scorer."""

    outcomes: list[FixtureOutcome] = field(default_factory=list)


def _build_verification_result(
    response: CapturedVerifierResponse,
) -> verifier.VerificationResult:
    """Reconstruct a :class:`VerificationResult` from a captured response."""
    return verifier.VerificationResult(
        verdict=response.verdict,
        explanation=response.explanation,
        sources=list(response.sources),
        correction=response.correction,
        grounded=response.grounded,
        model_used=response.model_used,
        cache_status=response.cache_status,
        web_search_requests=response.web_search_requests,
        successful_source_count=response.successful_source_count,
        search_error_count=response.search_error_count,
        verification_profile=response.verification_profile,
        verification_mode=response.verification_mode,
    )


def _build_finding(payload: FindingPayload) -> Finding:
    """Translate a fixture finding payload into a real :class:`Finding`."""
    return Finding(
        severity=payload.severity,
        fileName=payload.fileName,
        section=payload.section,
        issue=payload.issue,
        actionType=payload.actionType,
        existingText=payload.existingText,
        replacementText=payload.replacementText,
        codeReference=payload.codeReference,
        confidence=payload.confidence,
        anchorText=payload.anchorText,
        insertPosition=payload.insertPosition,
        evidenceElementId=payload.evidenceElementId,
    )


def _apply_grounding(
    result: verifier.VerificationResult,
    response: CapturedVerifierResponse,
) -> verifier.VerificationResult:
    """Replay the production grounding helpers.

    Local-skip results bypass the source-grounding helpers in production
    (``_local_skip_result`` is constructed inline and never flows through
    ``_apply_source_grounding``). The harness mirrors that bypass so a
    fixture marked ``cache_status=local_skip`` reaches the classifier
    unchanged.
    """
    if response.cache_status == _LOCAL_SKIP:
        return result
    searched = [SearchedSource(url=u, title="") for u in response.searched_urls]
    grounded = verifier._apply_source_grounding(result, searched=searched)
    return verifier._enforce_grounding_invariant(grounded)


def _apply_budget_exhaustion(
    result: verifier.VerificationResult,
    *,
    severity: str,
) -> verifier.VerificationResult:
    """Mirror the production budget-exhaustion detection.

    Chunk 13 / Trust Upgrade: the runtime sets
    ``VerificationResult.budget_exhausted=True`` when
    ``web_search_requests >= decision.web_search_max_uses`` AND the
    final verdict is UNVERIFIED. The calibration harness replays
    captured verifier responses (not live API calls) so this
    detection has to be applied here for fixtures that exercise the
    case to be observable through the scorer's metrics. The condition
    mirrors :func:`src.verification.verifier._run_verification_call`
    exactly so a fixture marked with ``web_search_requests`` at or
    above the severity-tiered budget surfaces the flag.

    Local-skip results bypass detection (they don't search at all);
    grounded supportive verdicts (CONFIRMED / CORRECTED) likewise are
    skipped — a grounded verdict that consumed the full budget is the
    model doing its job, not a shortfall.
    """
    if result.cache_status == _LOCAL_SKIP:
        return result
    if (result.verdict or "").strip().upper() != "UNVERIFIED":
        return result
    budget = web_search_max_uses_for_severity(severity)
    if budget and result.web_search_requests >= budget:
        result.budget_exhausted = True
    return result


def _resolve_search_budget(severity: str) -> int:
    """Return the per-severity web_search budget for context."""
    return web_search_max_uses_for_severity(severity) or DEFAULT_VERIFICATION_MAX_USES


def run_fixture(fixture: CalibrationFixture) -> FixtureOutcome:
    """Replay one fixture through grounding + classification."""
    response = fixture.captured_verifier_response
    captured_verdict = response.verdict
    result = _build_verification_result(response)
    grounded_result = _apply_grounding(result, response)
    # Chunk 13: apply budget-exhaustion detection AFTER grounding so a
    # CONFIRMED that was downgraded to UNVERIFIED still picks up the
    # flag when the model used its full search budget. Mirrors the
    # production ordering in ``_run_verification_call``.
    grounded_result = _apply_budget_exhaustion(
        grounded_result, severity=fixture.severity
    )

    finding = _build_finding(fixture.finding)
    finding.verification = grounded_result

    status = report_status.classify_status(finding)

    expected_verdict = fixture.ground_truth.correct_verdict
    expected_status = fixture.ground_truth.expected_status

    grounded_verdict = (grounded_result.verdict or "").strip().upper()
    verdict_match = grounded_verdict == expected_verdict
    status_match = (
        status.value == expected_status if expected_status is not None else None
    )

    issues: list[str] = []
    if not verdict_match:
        issues.append(
            f"verdict mismatch: expected {expected_verdict}, "
            f"got {grounded_verdict} (captured: {captured_verdict})"
        )
    if status_match is False:
        issues.append(
            f"status mismatch: expected {expected_status}, got {status.value}"
        )

    return FixtureOutcome(
        fixture_id=fixture.fixture_id,
        category=fixture.category,
        severity=fixture.severity,
        description=fixture.description,
        captured_verdict=captured_verdict,
        grounded_verdict=grounded_verdict,
        expected_verdict=expected_verdict,
        finding_confidence=float(fixture.finding.confidence),
        actual_status=status.value,
        expected_status=expected_status,
        cited_count=len(grounded_result.cited_sources),
        accepted_count=len(grounded_result.accepted_sources),
        grounded=bool(grounded_result.grounded),
        cache_status=grounded_result.cache_status,
        verdict_match=verdict_match,
        status_match=status_match,
        verification_mode=grounded_result.verification_mode,
        verification_profile=grounded_result.verification_profile,
        web_search_requests=int(response.web_search_requests),
        web_search_budget=_resolve_search_budget(fixture.severity),
        budget_exhausted=bool(grounded_result.budget_exhausted),
        notes=fixture.ground_truth.notes,
        issues=issues,
    )


def run_harness(fixtures: Iterable[CalibrationFixture]) -> HarnessResult:
    """Run every fixture and return a :class:`HarnessResult`."""
    return HarnessResult(outcomes=[run_fixture(fx) for fx in fixtures])
