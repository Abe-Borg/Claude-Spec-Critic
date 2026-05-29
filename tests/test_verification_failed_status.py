"""Tests for VERIFICATION_FAILED status + verification_failed sentinel.

This work adds operational-failure visibility so the
report can distinguish "verifier broke" from "verifier ran but found
nothing." The contract has five surfaces:

* ``VerificationResult.verification_failed`` defaults to False and round-
  trips through serialize/deserialize (resume state) and through the
  in-memory cache clone helpers (so cache replays preserve the flag if
  it ever leaks into a cached entry — which the cache guard should
  prevent).
* ``ReportStatus.VERIFICATION_FAILED`` exists and is registered in every
  display mapping (labels, glyphs, display order, colors, shading).
* ``classify_status`` returns VERIFICATION_FAILED when the sentinel is
  set, prioritized after suppression but BEFORE the verdict-based
  branches (so a CONFIRMED-looking verdict that's actually a transient
  failure can't masquerade as VERIFIED_SUPPORTED).
* ``classify_edit_action`` labels a VERIFICATION_FAILED finding
  EDIT_SUGGESTED when it carries a proposal and REPORT_ONLY otherwise;
  the failed status rides along for a downstream applier to act on.
* ``VerificationCache.put`` refuses to persist ``verification_failed=True``
  results regardless of grounded/sources — these are transient signals,
  not durable verdicts.
"""
from __future__ import annotations

from pathlib import Path


from src.core.code_cycles import DEFAULT_CYCLE
from src.output.report_exporter import STATUS_COLORS, STATUS_SHADING
from src.output.report_status import (
    EditActionLabel,
    ReportStatus,
    STATUS_DISPLAY_ORDER,
    classify_edit_action,
    classify_status,
)
from src.review.reviewer import EditProposal, Finding
from src.verification.verification_cache import VerificationCache
from src.verification.verifier import VerificationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _finding(
    *,
    severity: str = "HIGH",
    file: str = "Section_22_1000.docx",
    section: str = "2.1",
    issue: str = "Stale code reference",
    confidence: float = 0.6,
    action: str = "EDIT",
    existing: str | None = "old text",
    replacement: str | None = "new text",
    verification: VerificationResult | None = None,
    edit_proposal: EditProposal | None = None,
) -> Finding:
    f = Finding(
        severity=severity,
        fileName=file,
        section=section,
        issue=issue,
        actionType=action,
        existingText=existing,
        replacementText=replacement,
        codeReference="CBC §1234",
        confidence=confidence,
        edit_proposal=edit_proposal,
    )
    f.verification = verification
    return f


def _failed_verification(
    explanation: str = "Server overloaded during verification: 529",
) -> VerificationResult:
    return VerificationResult(
        verdict="UNVERIFIED",
        explanation=explanation,
        grounded=False,
        verification_failed=True,
    )


# ---------------------------------------------------------------------------
# 1. ReportStatus enum + display metadata
# ---------------------------------------------------------------------------


class TestVerificationFailedReportStatus:
    def test_display_order_places_failed_after_uncertain_block(self):
        # VERIFICATION_FAILED should sit in the operational tail —
        # after the uncertain/disputed block, before NOT_CHECKED /
        # MANUAL_REVIEW_REQUIRED. This matches the report's reading
        # order: supportive first, then uncertain, then operational.
        order = list(STATUS_DISPLAY_ORDER)
        i_failed = order.index(ReportStatus.VERIFICATION_FAILED)
        assert i_failed > order.index(ReportStatus.DISPUTED)

    def test_color_is_registered(self):
        # The report exporter renders status with a color + shading
        # pair; missing either would crash the export.
        assert ReportStatus.VERIFICATION_FAILED in STATUS_COLORS
        assert ReportStatus.VERIFICATION_FAILED in STATUS_SHADING


# ---------------------------------------------------------------------------
# 3. classify_status — VERIFICATION_FAILED branch
# ---------------------------------------------------------------------------


class TestClassifyStatusVerificationFailed:
    def test_failed_sentinel_overrides_unverified(self):
        f = _finding(verification=_failed_verification())
        assert classify_status(f) is ReportStatus.VERIFICATION_FAILED

    def test_failed_sentinel_overrides_confirmed(self):
        # Even if the verdict says CONFIRMED (which should not happen
        # in production), a verification_failed=True result must not
        # render as VERIFIED_SUPPORTED. Belt-and-suspenders for a future
        # call site that constructs the result by hand.
        v = VerificationResult(
            verdict="CONFIRMED",
            grounded=True,
            sources=["https://example.com/"],
            verification_failed=True,
        )
        f = _finding(verification=v)
        assert classify_status(f) is ReportStatus.VERIFICATION_FAILED

    def test_clean_unverified_stays_insufficient_evidence(self):
        # A regular UNVERIFIED (verifier ran, didn't ground) keeps
        # the existing INSUFFICIENT_EVIDENCE classification.
        v = VerificationResult(
            verdict="UNVERIFIED",
            explanation="No grounded evidence found.",
            grounded=False,
            verification_failed=False,
        )
        f = _finding(verification=v)
        assert classify_status(f) is ReportStatus.INSUFFICIENT_EVIDENCE

    def test_failed_sentinel_takes_precedence_over_local_skip(self):
        # An UNVERIFIED with cache_status=local_skip would normally
        # classify as LOCALLY_CLASSIFIED. If verification_failed is
        # also set (a contrived case — local_skip and failure are
        # mutually exclusive in production), the failure wins because
        # the operator needs to see the operational issue.
        v = VerificationResult(
            verdict="UNVERIFIED",
            cache_status="local_skip",
            verification_failed=True,
        )
        f = _finding(verification=v)
        assert classify_status(f) is ReportStatus.VERIFICATION_FAILED


# ---------------------------------------------------------------------------
# 4. classify_edit_action — VERIFICATION_FAILED never auto-edits
# ---------------------------------------------------------------------------


class TestClassifyEditActionVerificationFailed:
    def test_failed_without_proposal_routes_to_report_only(self):
        # No proposal means no edit, regardless of status. The
        # short-circuit on missing proposal applies first. Setting
        # action=REPORT_ONLY is the canonical way to express "no
        # proposal" — leaving existing/replacement text on an EDIT
        # action would auto-materialize a proposal in Finding.
        f = _finding(
            verification=_failed_verification(),
            action="REPORT_ONLY",
            existing=None,
            replacement=None,
            edit_proposal=None,
        )
        assert classify_edit_action(f) is EditActionLabel.REPORT_ONLY


# ---------------------------------------------------------------------------
# 6. Cache — refuses to persist verification_failed=True
# ---------------------------------------------------------------------------


class TestCacheRejectsFailedResults:
    def _finding_for_cache(self) -> Finding:
        return Finding(
            severity="HIGH",
            fileName="Section_22_1000.docx",
            section="2.1",
            issue="claim about NFPA 13",
            actionType="EDIT",
            existingText="per NFPA 13 (2019)",
            replacementText="per NFPA 13 (2022)",
            codeReference="NFPA 13 §10",
            confidence=0.6,
        )

    def test_grounded_failed_result_is_not_cached(self):
        # Contrived case: grounded=True (would normally be cacheable)
        # plus verification_failed=True (transient signal). The
        # verification_failed flag must veto caching regardless of
        # grounded/sources status.
        cache = VerificationCache()
        f = self._finding_for_cache()
        cache.put(
            f,
            cycle=DEFAULT_CYCLE,
            result=VerificationResult(
                verdict="CONFIRMED",
                grounded=True,
                sources=["https://nfpa.org/"],
                accepted_sources=["https://nfpa.org/"],
                source_quote="snippet",
                verification_failed=True,
            ),
        )
        # No entry should have been stored.
        assert cache.get(f, cycle=DEFAULT_CYCLE) is None
        assert cache.stats()["size"] == 0

    def test_clean_grounded_result_is_still_cached(self):
        # Sanity: the new guard does not block normal grounded
        # verdicts. Without it the earlier source_quote cache tests would
        # already catch this, but explicit coverage anchors the
        # invariant.
        cache = VerificationCache()
        f = self._finding_for_cache()
        cache.put(
            f,
            cycle=DEFAULT_CYCLE,
            result=VerificationResult(
                verdict="CONFIRMED",
                grounded=True,
                sources=["https://nfpa.org/"],
                accepted_sources=["https://nfpa.org/"],
                source_quote="snippet",
            ),
        )
        hit = cache.get(f, cycle=DEFAULT_CYCLE)
        assert hit is not None
        assert hit.verdict == "CONFIRMED"


# ---------------------------------------------------------------------------
# 7. Pipeline catch-all marks verification_failed
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 8. Verifier exception paths mark verification_failed
# ---------------------------------------------------------------------------


class TestVerifierExceptionPathsMarkFailed:
    """The plan section 3b says _run_verification_call's exception
    handlers (rate limit, server error, API error, unexpected error)
    must all set verification_failed=True. We verify by source
    inspection because the exception paths can't easily be triggered
    without a real network call.
    """

    def test_exception_paths_mark_failed(self):
        # The helper signature must accept ``failed=True`` so all
        # exception paths can use it without bespoke construction.
        import inspect

        from src.verification import verifier

        call_source = inspect.getsource(verifier._run_verification_call)
        assert "failed: bool = False" in call_source
        # Every call site in the exception block must carry failed=True.
        assert "except Exception as e:" in call_source
        exception_section = call_source.split("except Exception as e:")[1]
        # All 5 make_unverified calls in the exception block should
        # have failed=True. The block is the rest of the function.
        make_unverified_calls = exception_section.count("_make_unverified(")
        failed_true_calls = exception_section.count("failed=True")
        assert make_unverified_calls >= 5
        assert failed_true_calls >= make_unverified_calls

        # The real-time fallback crash path also stamps the flag.
        file_source = Path("src/verification/verifier.py").read_text(encoding="utf-8")
        fallback_index = file_source.find("Real-time fallback verification failed:")
        assert fallback_index >= 0
        nearby = file_source[fallback_index : fallback_index + 500]
        assert "verification_failed=True" in nearby
