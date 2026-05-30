"""Tests for cross-checker CSI-division chunking (TRUST_AUDIT P1-3).

The chunked cross-check path (`run_chunked_cross_check`) splits a large
project into per-CSI-division chunks so a megaproject still gets a
coordination pass instead of an all-or-nothing ``skipped``. The audit asks
two things:

1. **No silent loss / mis-attribution across chunk boundaries.** Every spec
   must land in exactly one chunk (no drop, no duplication), and a finding
   from one chunk must not be attributed to another chunk's discipline.
2. **Cross-division coordination spanning chunks must be detected — or the
   limitation must be known.** It is NOT detected (each chunk is cross-checked
   in isolation), so these tests lock in that *known* limitation explicitly
   alongside the completeness guarantees.

They also pin the partial-failure behavior: a failed chunk never drops the
other chunks' findings, and the combined status follows the documented rule
(``completed`` when ≥1 chunk completed; ``failed``/``skipped`` only when zero
completed).
"""
from __future__ import annotations

import pytest

import src.cross_check.cross_checker as cc
from src.cross_check.cross_checker import (
    _assign_chunk,
    _chunk_label,
    _group_specs_by_chunk,
    _synthesize_chunk_findings,
    run_chunked_cross_check,
)
from src.core.code_cycles import DEFAULT_CYCLE
from src.input.extractor import ExtractedSpec
from src.review.reviewer import Finding, ReviewResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(filename: str) -> ExtractedSpec:
    return ExtractedSpec(
        filename=filename,
        content=f"Section content for {filename}. Provide equipment per code.",
        word_count=8,
    )


def _finding(filename: str, *, section: str = "2.1", issue: str = "coord") -> Finding:
    return Finding(
        severity="MEDIUM",
        fileName=filename,
        section=section,
        issue=issue,
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference="",
    )


def _chunk_result(
    status: str,
    *,
    findings: list[Finding] | None = None,
    error: str = "",
    thinking: str = "",
) -> ReviewResult:
    return ReviewResult(
        findings=findings or [],
        thinking=thinking,
        model="fake",
        cross_check_status=status,
        error=error,
        input_tokens=1,
        output_tokens=1,
    )


def _force_chunking(monkeypatch) -> None:
    """Make the token preflight always exceed the chunking threshold so
    `run_chunked_cross_check` takes the chunked path with small fixtures."""
    monkeypatch.setattr(cc, "count_tokens", lambda *_a, **_k: cc.CROSS_CHECK_RECOMMENDED_MAX)


# ===========================================================================
# 1. Chunk assignment + completeness — no spec is ever dropped
# ===========================================================================


class TestChunkAssignment:
    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("21 13 00 - Sprinklers.docx", "div_21"),
            ("22 11 00 - Domestic Water.docx", "div_22"),
            ("23 05 00 - HVAC Common.docx", "div_23"),
            ("25 90 00 - Sequences.docx", "controls_commissioning"),
            ("01 91 00 - Commissioning.docx", "controls_commissioning"),
        ],
    )
    def test_known_divisions_route_to_their_chunk(self, filename, expected):
        assert _assign_chunk(filename) == expected

    @pytest.mark.parametrize(
        "filename",
        [
            "Cover Sheet.docx",          # no CSI prefix
            "99 99 00 - Unknown.docx",   # parseable prefix, not in any group
            "",                           # empty
        ],
    )
    def test_unmatched_filenames_fall_back_to_general(self, filename):
        # Never dropped: anything unrecognized pools into "general".
        assert _assign_chunk(filename) == "general"


class TestChunkCompleteness:
    def test_every_spec_lands_in_exactly_one_chunk(self):
        specs = [
            _spec("22 11 00 - Water.docx"),
            _spec("22 13 00 - Sanitary.docx"),
            _spec("23 05 00 - HVAC.docx"),
            _spec("23 07 00 - Insulation.docx"),
            _spec("21 13 00 - Sprinklers.docx"),  # singleton division
            _spec("Cover.docx"),                   # unparseable
        ]
        chunks = _group_specs_by_chunk(specs)

        # The union of every chunk's specs equals the input set — no drop.
        grouped = [s for _cid, group in chunks for s in group]
        assert len(grouped) == len(specs), "a spec was dropped or duplicated"
        assert {s.filename for s in grouped} == {s.filename for s in specs}

        # No spec appears in two chunks.
        seen: set[str] = set()
        for _cid, group in chunks:
            for s in group:
                assert s.filename not in seen, f"{s.filename} in two chunks"
                seen.add(s.filename)

    def test_div_22_and_div_23_are_separate_chunks(self):
        specs = [
            _spec("22 11 00 - Water.docx"),
            _spec("22 13 00 - Sanitary.docx"),
            _spec("23 05 00 - HVAC.docx"),
            _spec("23 07 00 - Insulation.docx"),
        ]
        chunks = dict(_group_specs_by_chunk(specs))
        assert set(chunks) == {"div_22", "div_23"}
        assert len(chunks["div_22"]) == 2
        assert len(chunks["div_23"]) == 2

    def test_singleton_divisions_pool_into_general(self):
        # A lone Division 21 spec has no second spec to coordinate against,
        # so it merges into "general" rather than forming a 1-spec chunk.
        specs = [
            _spec("22 11 00 - Water.docx"),
            _spec("22 13 00 - Sanitary.docx"),
            _spec("21 13 00 - Sprinklers.docx"),  # singleton
        ]
        chunks = dict(_group_specs_by_chunk(specs))
        assert "div_21" not in chunks
        assert "general" in chunks
        assert [s.filename for s in chunks["general"]] == ["21 13 00 - Sprinklers.docx"]


# ===========================================================================
# 2. Cross-division coordination across chunks is NOT detectable (known limit)
# ===========================================================================


class TestCrossDivisionLimitationIsKnown:
    def test_no_single_call_sees_two_divisions(self, monkeypatch):
        """Each chunk is cross-checked in isolation. Lock in that a Division 22
        spec and a Division 23 spec never appear in the same run_cross_check
        call, so a cross-division conflict cannot be seen when chunked."""
        _force_chunking(monkeypatch)
        seen_sets: list[set[str]] = []

        def fake_run_cross_check(specs, _existing, **_kwargs):
            seen_sets.append({s.filename for s in specs})
            return _chunk_result("completed")

        monkeypatch.setattr(cc, "run_cross_check", fake_run_cross_check)

        specs = [
            _spec("22 11 00 - Water.docx"),
            _spec("22 13 00 - Sanitary.docx"),
            _spec("23 05 00 - HVAC.docx"),
            _spec("23 07 00 - Insulation.docx"),
        ]
        run_chunked_cross_check(specs, [], cycle=DEFAULT_CYCLE)

        assert len(seen_sets) == 2, "expected one call per division chunk"
        for filenames in seen_sets:
            has_22 = any(f.startswith("22") for f in filenames)
            has_23 = any(f.startswith("23") for f in filenames)
            assert not (has_22 and has_23), (
                "a single cross-check call saw two divisions — the cross-division "
                "limitation no longer holds; update the docs/tests deliberately."
            )

    def test_small_project_takes_unchunked_path(self, monkeypatch):
        # Within the token limit, the wrapper delegates to a single
        # run_cross_check over ALL specs (no cross-division blind spot).
        monkeypatch.setattr(cc, "count_tokens", lambda *_a, **_k: 10)
        seen_sets: list[set[str]] = []

        def fake_run_cross_check(specs, _existing, **_kwargs):
            seen_sets.append({s.filename for s in specs})
            return _chunk_result("completed")

        monkeypatch.setattr(cc, "run_cross_check", fake_run_cross_check)
        specs = [_spec("22 11 00 - Water.docx"), _spec("23 05 00 - HVAC.docx")]
        run_chunked_cross_check(specs, [], cycle=DEFAULT_CYCLE)

        assert len(seen_sets) == 1
        assert seen_sets[0] == {"22 11 00 - Water.docx", "23 05 00 - HVAC.docx"}


# ===========================================================================
# 3. Partial chunk failure: other chunks' findings survive; no mis-attribution
# ===========================================================================


class TestPartialChunkFailure:
    def test_failed_chunk_does_not_drop_completed_chunk_findings(self, monkeypatch):
        _force_chunking(monkeypatch)

        def fake_run_cross_check(specs, _existing, **_kwargs):
            filenames = {s.filename for s in specs}
            if any(f.startswith("22") for f in filenames):
                return _chunk_result(
                    "completed", findings=[_finding("22 11 00 - Water.docx")]
                )
            return _chunk_result("failed", error="boom: API 500")

        monkeypatch.setattr(cc, "run_cross_check", fake_run_cross_check)
        specs = [
            _spec("22 11 00 - Water.docx"),
            _spec("22 13 00 - Sanitary.docx"),
            _spec("23 05 00 - HVAC.docx"),
            _spec("23 07 00 - Insulation.docx"),
        ]
        combined = run_chunked_cross_check(specs, [], cycle=DEFAULT_CYCLE)

        # The completed chunk's finding survives the partial failure.
        assert len(combined.findings) == 1
        # Documented rule: completed when ≥1 chunk completed.
        assert combined.cross_check_status == "completed"
        # The failure is tallied in the summary, not silently swallowed.
        assert "1 completed, 1 failed" in combined.thinking

    def test_surviving_finding_keeps_its_own_division_label(self, monkeypatch):
        # No mis-attribution: the Division 22 finding is labeled Division 22,
        # never Division 23 (the chunk that failed).
        _force_chunking(monkeypatch)

        def fake_run_cross_check(specs, _existing, **_kwargs):
            filenames = {s.filename for s in specs}
            if any(f.startswith("22") for f in filenames):
                return _chunk_result(
                    "completed", findings=[_finding("22 11 00 - Water.docx")]
                )
            return _chunk_result("failed", error="boom")

        monkeypatch.setattr(cc, "run_cross_check", fake_run_cross_check)
        specs = [
            _spec("22 11 00 - Water.docx"),
            _spec("22 13 00 - Sanitary.docx"),
            _spec("23 05 00 - HVAC.docx"),
            _spec("23 07 00 - Insulation.docx"),
        ]
        combined = run_chunked_cross_check(specs, [], cycle=DEFAULT_CYCLE)
        section = combined.findings[0].section
        assert _chunk_label("div_22") in section
        assert _chunk_label("div_23") not in section


# ===========================================================================
# 4. Status synthesis rules (pure function — the documented matrix)
# ===========================================================================


class TestSynthesisStatusMatrix:
    def test_at_least_one_completed_is_completed(self):
        results = [
            ("div_22", _chunk_result("completed", findings=[_finding("22 11 00.docx")])),
            ("div_23", _chunk_result("failed", error="x")),
        ]
        findings, _summary, status = _synthesize_chunk_findings(
            results, fallback_model="m", cycle=DEFAULT_CYCLE
        )
        assert status == "completed"
        assert len(findings) == 1

    def test_zero_completed_with_failures_is_failed(self):
        results = [
            ("div_22", _chunk_result("failed", error="x")),
            ("div_23", _chunk_result("failed", error="y")),
        ]
        findings, summary, status = _synthesize_chunk_findings(
            results, fallback_model="m", cycle=DEFAULT_CYCLE
        )
        assert status == "failed"
        assert findings == []
        assert "0 completed, 2 failed" in summary

    def test_zero_completed_only_skipped_is_skipped(self):
        results = [
            ("div_22", _chunk_result("skipped", thinking="too small")),
            ("div_23", _chunk_result("skipped", thinking="too small")),
        ]
        _findings, _summary, status = _synthesize_chunk_findings(
            results, fallback_model="m", cycle=DEFAULT_CYCLE
        )
        assert status == "skipped"
