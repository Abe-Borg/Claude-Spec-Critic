"""Tests for the strengthened source-grounding invariant.

An *externally* verified ``CONFIRMED`` /
``CORRECTED`` result must carry at least one **accepted external
citation**. The prior invariant only required
``grounded=True`` — which is satisfied by any successful web_search
block — so a model that emitted ``CONFIRMED`` with ``sources=[]``
slipped through. That is an audit liability for the report.

The strengthened invariant is implemented in two pieces:

* :func:`src.verifier._enforce_grounding_invariant` downgrades any
  ``CONFIRMED`` / ``CORRECTED`` whose ``accepted_sources`` *and*
  legacy ``sources`` lists are both empty. The explanation gets a
  short "no accepted external citation was provided" suffix.
* :class:`src.verification_cache.VerificationCache` refuses to put
  source-less verdicts, bumps the on-disk schema to v2, and
  re-validates the invariant on load. That way an older cache file
  cannot silently bypass the new rule on a cache hit.

The tests below pin down the contract: well-grounded verdicts pass
through, source-less verdicts downgrade with a clear reason, local
skips are exempt, and the cache cannot resurrect a pre-Chunk-5
source-less entry.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.core.code_cycles import DEFAULT_CYCLE
from src.review.reviewer import Finding
from src.verification.source_grounding import SearchedSource
from src.verification.verifier import (
    VerificationResult,
    _apply_source_grounding,
    _enforce_grounding_invariant,
    _local_skip_result,
)
from src.verification.verification_cache import VerificationCache, _CACHE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _finding(
    *,
    severity: str = "HIGH",
    issue: str = "code-cycle staleness",
    code_ref: str | None = "CBC 2025 §1004",
    existing: str | None = "per CBC 2019",
    replacement: str | None = "per CBC 2025",
    action: str = "EDIT",
    section: str = "2.1",
    filename: str = "23 21 13 - Hydronic.docx",
) -> Finding:
    return Finding(
        severity=severity,
        fileName=filename,
        section=section,
        issue=issue,
        actionType=action,
        existingText=existing,
        replacementText=replacement,
        codeReference=code_ref,
        confidence=0.6,
    )


# ===========================================================================
# 1. The strengthened invariant itself
# ===========================================================================


class TestEnforceGroundingInvariantWithAcceptedCitations:
    """Direct unit tests of :func:`_enforce_grounding_invariant`."""

    @pytest.mark.parametrize("verdict", ["CONFIRMED", "CORRECTED"])
    def test_verified_verdict_with_accepted_citation_survives(self, verdict: str):
        r = VerificationResult(
            verdict=verdict,
            grounded=True,
            accepted_sources=["https://dgs.ca.gov/example"],
            sources=["https://dgs.ca.gov/example"],
            correction="2025 CBC, not 2019" if verdict == "CORRECTED" else None,
        )
        out = _enforce_grounding_invariant(r)
        assert out.verdict == verdict
        assert out.grounded is True
        if verdict == "CORRECTED":
            # CORRECTED's correction text must survive the invariant pass.
            assert out.correction == "2025 CBC, not 2019"

    @pytest.mark.parametrize("verdict", ["CONFIRMED", "CORRECTED"])
    def test_verified_verdict_with_empty_sources_downgrades(self, verdict: str):
        """No accepted citation AND no legacy sources → UNVERIFIED."""
        r = VerificationResult(
            verdict=verdict,
            grounded=True,  # search blocks succeeded
            accepted_sources=[],
            sources=[],
            correction="The standard is 2025." if verdict == "CORRECTED" else None,
        )
        out = _enforce_grounding_invariant(r)
        assert out.verdict == "UNVERIFIED"
        if verdict == "CONFIRMED":
            assert out.grounded is False
        # The explanation has to say WHY so reviewers can audit the
        # downgrade rather than seeing a silent UNVERIFIED.
        assert "no accepted external citation" in out.explanation.lower()

    def test_legacy_sources_alone_satisfies_invariant(self):
        """A pre-Chunk-H caller that only populated ``sources`` must still pass.

        Production paths keep ``accepted_sources`` and ``sources`` in
        sync via :func:`_apply_source_grounding`, but unit tests and
        legacy callers may set only ``sources``. Either field is treated
        as evidence of an accepted citation.
        """
        r = VerificationResult(
            verdict="CONFIRMED",
            grounded=True,
            sources=["https://example.com"],
            accepted_sources=[],
        )
        out = _enforce_grounding_invariant(r)
        assert out.verdict == "CONFIRMED"

    def test_ungrounded_confirmed_still_downgrades(self):
        """The original Phase 3 invariant still fires for ``not grounded``."""
        r = VerificationResult(
            verdict="CONFIRMED",
            grounded=False,
            explanation="Search returned no results.",
        )
        out = _enforce_grounding_invariant(r)
        assert out.verdict == "UNVERIFIED"
        assert "external grounding" in out.explanation.lower()

    def test_unverified_with_sources_unchanged(self):
        """UNVERIFIED is not in the downgrade branch."""
        r = VerificationResult(
            verdict="UNVERIFIED",
            grounded=False,
            accepted_sources=[],
            sources=[],
            explanation="No search performed.",
        )
        out = _enforce_grounding_invariant(r)
        assert out.verdict == "UNVERIFIED"

    def test_disputed_with_no_sources_unchanged(self):
        """DISPUTED is allowed without accepted citations (it's not 'verified')."""
        r = VerificationResult(
            verdict="DISPUTED",
            grounded=True,
            accepted_sources=[],
            sources=[],
            explanation="Sources contradict each other.",
        )
        out = _enforce_grounding_invariant(r)
        assert out.verdict == "DISPUTED"

    def test_downgrade_explanation_not_double_appended(self):
        """Running the invariant twice keeps the explanation idempotent."""
        r = VerificationResult(
            verdict="CONFIRMED", grounded=True, accepted_sources=[], sources=[]
        )
        once = _enforce_grounding_invariant(r)
        twice = _enforce_grounding_invariant(once)
        # The suffix appears once even after a second pass.
        suffix_count = twice.explanation.lower().count(
            "no accepted external citation"
        )
        assert suffix_count == 1


# ===========================================================================
# 2. Composition with _apply_source_grounding (the production flow)
# ===========================================================================


class TestProductionGroundingFlow:
    """``_apply_source_grounding`` then ``_enforce_grounding_invariant``.

    Production verifier paths (real-time + batch wave) call the two
    helpers in this order. Tests below exercise the chain end-to-end so
    a future refactor that breaks the composition will fail loudly.
    """

    def _run_chain(
        self, r: VerificationResult, searched: list[SearchedSource]
    ) -> VerificationResult:
        out = _apply_source_grounding(r, searched=searched)
        return _enforce_grounding_invariant(out)

    @pytest.mark.parametrize("verdict", ["CONFIRMED", "CORRECTED"])
    def test_verified_verdict_with_accepted_citation_survives(self, verdict: str):
        r = VerificationResult(
            verdict=verdict,
            sources=["https://dgs.ca.gov/page"],
            grounded=True,
            correction="2025 cycle, not 2019." if verdict == "CORRECTED" else None,
        )
        searched = [SearchedSource(url="https://dgs.ca.gov/page", title="DGS")]
        out = self._run_chain(r, searched)
        assert out.verdict == verdict
        assert out.accepted_sources == ["https://dgs.ca.gov/page"]
        assert out.rejected_sources == []
        # Public ``sources`` is replaced by accepted_sources only.
        assert out.sources == ["https://dgs.ca.gov/page"]
        if verdict == "CORRECTED":
            assert out.correction == "2025 cycle, not 2019."

    def test_confirmed_with_only_invented_source_downgrades(self):
        """Every cited URL missing from search results → downgrade."""
        r = VerificationResult(
            verdict="CONFIRMED",
            sources=["https://invented.example.com/fake"],
            grounded=True,
            explanation="DGS says it's current.",
        )
        searched = [SearchedSource(url="https://dgs.ca.gov/page")]
        out = self._run_chain(r, searched)
        assert out.verdict == "UNVERIFIED"
        assert out.accepted_sources == []
        assert len(out.rejected_sources) == 1
        # Either downgrade explanation is acceptable as long as it
        # mentions the cause.
        explanation = out.explanation.lower()
        assert "downgraded" in explanation
        assert (
            "did not appear in web_search results" in explanation
            or "no accepted external citation" in explanation
        )

    @pytest.mark.parametrize("verdict", ["CONFIRMED", "CORRECTED"])
    def test_verified_verdict_with_no_citations_downgrades_via_invariant(
        self, verdict: str
    ):
        """Search ran successfully but the model emitted no citations.

        Before the strengthened invariant this slipped through:
        ``_apply_source_grounding`` does not touch the verdict when
        ``cited_sources`` is empty, and ``_enforce_grounding_invariant``
        only checked ``grounded``. The strengthened invariant catches it
        for both verified verdicts.
        """
        r = VerificationResult(
            verdict=verdict,
            sources=[],
            grounded=True,
            explanation="Looks fine.",
            correction="should be 2025" if verdict == "CORRECTED" else None,
        )
        searched = [SearchedSource(url="https://dgs.ca.gov/page")]
        out = self._run_chain(r, searched)
        assert out.verdict == "UNVERIFIED"
        if verdict == "CONFIRMED":
            assert out.grounded is False
            assert "no accepted external citation" in out.explanation.lower()
            # Searched sources are still recorded for diagnostics.
            assert out.searched_sources == ["https://dgs.ca.gov/page"]
        else:
            # The correction field is preserved on the result even when
            # the verdict is downgraded — diagnostics may want to see
            # what the model intended.
            assert out.correction == "should be 2025"


# ===========================================================================
# 3. Local-skip findings remain local-only and clearly labeled
# ===========================================================================


class TestLocalSkipExempt:
    def test_local_skip_result_is_unverified_with_marker(self):
        r = _local_skip_result()
        assert r.verdict == "UNVERIFIED"
        assert r.cache_status == "local_skip"
        # The invariant should not touch a local-skip result — it's
        # already UNVERIFIED, the CONFIRMED/CORRECTED branch is the only
        # one that downgrades.
        out = _enforce_grounding_invariant(r)
        assert out.verdict == "UNVERIFIED"
        assert out.cache_status == "local_skip"

    def test_local_skip_classifies_as_locally_classified_status(self):
        """Reports must distinguish local skips from web-confirmed findings."""
        from src.output.report_status import ReportStatus, classify_status

        f = _finding()
        f.verification = _local_skip_result()
        assert classify_status(f) is ReportStatus.LOCALLY_CLASSIFIED

    def test_local_skip_is_not_externally_confirmed(self):
        """A local-skip never carries a CONFIRMED verdict, so the new
        invariant cannot accidentally promote it."""
        r = _local_skip_result()
        # Hand-set CONFIRMED to defend against a future bug — even then
        # the invariant should downgrade because there is no accepted
        # citation. (Local skip never has sources.)
        r.verdict = "CONFIRMED"
        r.grounded = True
        out = _enforce_grounding_invariant(r)
        assert out.verdict == "UNVERIFIED"


# ===========================================================================
# 4. Cache: persisted entries cannot bypass the invariant
# ===========================================================================


class TestVerificationCacheInvariant:
    def test_schema_version_is_at_least_v2(self):
        """The bump invalidates older entries that may have stored
        source-less CONFIRMED results. Subsequent bumps (the v3
        source_quote bump) only tighten the invariant further."""
        assert _CACHE_SCHEMA_VERSION >= 2

    @pytest.mark.parametrize("verdict", ["CONFIRMED", "CORRECTED"])
    def test_cache_put_refuses_source_less_verified_verdict(self, verdict: str):
        cache = VerificationCache()
        f = _finding()
        # Source-less verified verdict — would have been stored under the
        # old rule ("grounded is enough"). Now silently refused for both
        # CONFIRMED and CORRECTED.
        cache.put(
            f,
            cycle=DEFAULT_CYCLE,
            result=VerificationResult(
                verdict=verdict,
                grounded=True,
                accepted_sources=[],
                sources=[],
                correction="It's 2025" if verdict == "CORRECTED" else None,
            ),
        )
        assert cache.get(f, cycle=DEFAULT_CYCLE) is None
        # The miss counter increments on get; put-rejection is silent.
        assert cache.stats()["size"] == 0

    def test_cache_put_accepts_source_bearing_confirmed(self):
        cache = VerificationCache()
        f = _finding()
        cache.put(
            f,
            cycle=DEFAULT_CYCLE,
            result=VerificationResult(
                verdict="CONFIRMED",
                grounded=True,
                accepted_sources=["https://dgs.ca.gov/page"],
                sources=["https://dgs.ca.gov/page"],
            ),
        )
        hit = cache.get(f, cycle=DEFAULT_CYCLE)
        assert hit is not None
        assert hit.verdict == "CONFIRMED"
        assert hit.accepted_sources == ["https://dgs.ca.gov/page"]
        assert hit.cache_status == "hit"

    def test_cache_load_drops_v1_files(self, tmp_path: Path, monkeypatch):
        """A v1 cache file on disk is silently ignored (schema bump)."""
        cache_path = tmp_path / "cache.json"
        v1_payload = {
            "version": 1,
            "saved_at": time.time(),
            "entries": {
                "legacy_key": {
                    "created_ts": time.time(),
                    "result": {
                        "verdict": "CONFIRMED",
                        "grounded": True,
                        "sources": [],  # pre-Chunk-5 source-less entry
                        "accepted_sources": [],
                        "explanation": "legacy",
                        "model_used": "claude-sonnet-4-6",
                        "escalated": False,
                        "web_search_requests": 1,
                        "successful_source_count": 1,
                        "search_error_count": 0,
                        "correction": None,
                    },
                },
            },
        }
        cache_path.write_text(json.dumps(v1_payload), encoding="utf-8")
        cache = VerificationCache()
        loaded = cache.load_from_disk(path=cache_path)
        # v1 entries are skipped wholesale: the version mismatch returns
        # 0 from load_from_disk, so no entries reach the per-entry
        # validation.
        assert loaded == 0
        assert cache.stats()["size"] == 0

    def test_cache_load_rejects_source_less_current_version_entry(
        self, tmp_path: Path, monkeypatch
    ):
        """Even a current-schema entry that violates the invariant must be rejected.

        Belt-and-suspenders: if some path manages to write a
        current-schema entry that violates the invariant (e.g. a future
        bug), the load-time check refuses to resurrect it. Production
        never produces such entries because the verifier downgrades them
        before write. The test uses ``_CACHE_SCHEMA_VERSION`` so future
        bumps (e.g. the v3 source_quote bump) don't break it.
        """
        cache_path = tmp_path / "cache.json"
        bad = {
            "version": _CACHE_SCHEMA_VERSION,
            "saved_at": time.time(),
            "entries": {
                "bad_key": {
                    "created_ts": time.time(),
                    "result": {
                        "verdict": "CONFIRMED",
                        "grounded": True,
                        "sources": [],
                        "accepted_sources": [],
                        "explanation": "stale",
                        "model_used": "claude-sonnet-4-6",
                        "escalated": False,
                        "web_search_requests": 1,
                        "successful_source_count": 1,
                        "search_error_count": 0,
                        "correction": None,
                        "source_quote": "snippet text",
                    },
                },
                "good_key": {
                    "created_ts": time.time(),
                    "result": {
                        "verdict": "CONFIRMED",
                        "grounded": True,
                        "sources": ["https://dgs.ca.gov"],
                        "accepted_sources": ["https://dgs.ca.gov"],
                        "explanation": "fresh",
                        "model_used": "claude-sonnet-4-6",
                        "escalated": False,
                        "web_search_requests": 1,
                        "successful_source_count": 1,
                        "search_error_count": 0,
                        "correction": None,
                        "source_quote": "snippet text",
                    },
                },
            },
        }
        cache_path.write_text(json.dumps(bad), encoding="utf-8")
        cache = VerificationCache()
        loaded = cache.load_from_disk(path=cache_path)
        # Only the good entry survives.
        assert loaded == 1
        assert cache.stats()["size"] == 1


# ===========================================================================
# 5. Batch and real-time paths apply the same invariant
# ===========================================================================


class TestBatchAndRealtimePathParity:
    """The two production paths funnel through the same helper chain.

    Both ``_run_verification_call`` (real-time) and
    ``_classify_wave_results`` (batch wave) call
    ``_apply_source_grounding`` then ``_enforce_grounding_invariant``.
    The test below asserts that the chain is deterministic — given the
    same parsed verdict and searched sources, both paths produce
    byte-identical downgrade decisions. That's the property we need to
    keep batch and real-time results equivalent.
    """

    @pytest.mark.parametrize(
        "verdict,sources,searched_urls,expected_verdict",
        [
            (
                "CONFIRMED",
                ["https://dgs.ca.gov/page"],
                ["https://dgs.ca.gov/page"],
                "CONFIRMED",
            ),
            (
                "CORRECTED",
                ["https://dgs.ca.gov/page"],
                ["https://dgs.ca.gov/page"],
                "CORRECTED",
            ),
            # Cited but not searched → downgrade.
            (
                "CONFIRMED",
                ["https://invented.example.com"],
                ["https://dgs.ca.gov/page"],
                "UNVERIFIED",
            ),
            # No citations at all → downgrade.
            (
                "CONFIRMED",
                [],
                ["https://dgs.ca.gov/page"],
                "UNVERIFIED",
            ),
            # No citations + no search → downgrade (existing invariant).
            ("CONFIRMED", [], [], "UNVERIFIED"),
            # UNVERIFIED stays UNVERIFIED.
            ("UNVERIFIED", [], [], "UNVERIFIED"),
            # DISPUTED stays DISPUTED (not in downgrade branch).
            ("DISPUTED", [], ["https://dgs.ca.gov/page"], "DISPUTED"),
        ],
    )
    def test_chain_applies_uniformly(
        self,
        verdict: str,
        sources: list[str],
        searched_urls: list[str],
        expected_verdict: str,
    ):
        # Real-time path stamps grounded=True iff search blocks
        # succeeded; mirror that here.
        grounded = len(searched_urls) > 0
        r = VerificationResult(
            verdict=verdict, sources=list(sources), grounded=grounded
        )
        searched = [SearchedSource(url=u) for u in searched_urls]
        out = _apply_source_grounding(r, searched=searched)
        out = _enforce_grounding_invariant(out)
        assert out.verdict == expected_verdict


# ===========================================================================
# 6. Report exporter doesn't claim verified support without accepted citations
# ===========================================================================


class TestReportClassificationGuards:
    """Belt-and-suspenders on the report side.

    The verifier invariant already downgrades source-less verdicts in
    production. The report exporter classifies findings via
    :func:`src.report_status.classify_status`; that helper is the
    second line of defense for results that reach the report without
    flowing through the invariant (e.g. a future code path, a test).
    """

    @pytest.mark.parametrize("verdict", ["CONFIRMED", "CORRECTED"])
    def test_verified_verdict_no_accepted_sources_renders_as_insufficient_evidence(
        self, verdict: str
    ):
        from src.output.report_status import ReportStatus, classify_status

        # Hand-construct a result that violates the invariant — as if a
        # bug let it slip through. The classifier should still refuse to
        # promote either verified verdict to VERIFIED_SUPPORTED.
        f = _finding()
        f.verification = VerificationResult(
            verdict=verdict,
            grounded=True,
            accepted_sources=[],
            sources=[],
            correction="2025 cycle." if verdict == "CORRECTED" else None,
        )
        assert classify_status(f) is ReportStatus.INSUFFICIENT_EVIDENCE

    def test_confirmed_with_accepted_citation_renders_as_verified_supported(
        self,
    ):
        from src.output.report_status import ReportStatus, classify_status

        f = _finding()
        f.verification = VerificationResult(
            verdict="CONFIRMED",
            grounded=True,
            accepted_sources=["https://dgs.ca.gov"],
            sources=["https://dgs.ca.gov"],
        )
        assert classify_status(f) is ReportStatus.VERIFIED_SUPPORTED
