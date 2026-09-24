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
from src.core.chunked_pass import group_specs_by_chunk, synthesize_chunk_results
from src.core.code_cycles import DEFAULT_CYCLE
from src.cross_check.cross_checker import (
    _assign_chunk,
    _chunk_label,
    run_chunked_cross_check,
)
from src.input.extractor import ExtractedSpec
from src.modules import DEFAULT_MODULE
from src.review.reviewer import Finding, ReviewResult
from tests.fixtures.count_api import CountingClient, spec_blocks

# The chunk grouping/synthesis helpers moved into the shared engine
# (``core.chunked_pass``), which takes the module's chunk groups explicitly.
_GROUPS = DEFAULT_MODULE.cross_check_chunk_groups


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
    """Scripted count API: 1,000 tokens per spec in the request (plan WP-08).

    The full four-spec corpus (4,000) exceeds the patched ceiling (2,500)
    while every two-spec division chunk (2,000) fits, so
    `run_chunked_cross_check` takes the chunked path and runs every
    division. (A constant stub count can no longer force chunking: the
    budget sizes each chunk's own request, and a chunk that measures as
    large as the whole package would not be sent.)
    """
    client = CountingClient(lambda request: 1_000 * spec_blocks(request))
    monkeypatch.setattr(cc, "_get_client", lambda *_a, **_k: client)
    monkeypatch.setattr(cc, "CROSS_CHECK_RECOMMENDED_MAX", 2_500)


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
        chunks = group_specs_by_chunk(specs, _GROUPS)

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
        chunks = dict(group_specs_by_chunk(specs, _GROUPS))
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
        chunks = dict(group_specs_by_chunk(specs, _GROUPS))
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


class TestChunkSubsetNote:
    """A chunked call tells the model it is seeing one division of a larger
    package. Compliance and the drawing digest already framed their chunks
    this way; cross-check disclosed the limit to the operator (a log line)
    but never to the model, which could render an unqualified 'coordination
    is adequate' over content it structurally could not fully assess."""

    def test_unchunked_user_message_is_byte_identical_without_the_flag(self):
        plain = cc._get_cross_check_user_message("<corpus/>", 2, project_context="ctx")
        explicit = cc._get_cross_check_user_message(
            "<corpus/>", 2, project_context="ctx", chunk_subset=False
        )
        assert plain == explicit
        assert cc._CHUNK_SUBSET_NOTE not in plain

    def test_chunked_user_message_carries_the_note_before_the_final_task(self):
        msg = cc._get_cross_check_user_message("<corpus/>", 2, chunk_subset=True)
        assert cc._CHUNK_SUBSET_NOTE in msg
        assert (
            msg.index("<corpus/>")
            < msg.index(cc._CHUNK_SUBSET_NOTE)
            < msg.index("<final_task>")
        ), "the note sits after the corpus and before the closing task block"
        assert msg.rstrip().endswith("</final_task>")

    def test_chunked_runner_flags_every_chunk_call(self, monkeypatch):
        _force_chunking(monkeypatch)
        flags: list[bool] = []

        def fake_run_cross_check(specs, _existing, **kwargs):
            flags.append(kwargs.get("chunk_subset", False))
            return _chunk_result("completed")

        monkeypatch.setattr(cc, "run_cross_check", fake_run_cross_check)
        specs = [
            _spec("22 11 00 - Water.docx"),
            _spec("22 13 00 - Sanitary.docx"),
            _spec("23 05 00 - HVAC.docx"),
            _spec("23 07 00 - Insulation.docx"),
        ]
        run_chunked_cross_check(specs, [], cycle=DEFAULT_CYCLE)
        assert flags and all(flags)

    def test_unchunked_runner_never_sets_the_flag(self, monkeypatch):
        monkeypatch.setattr(cc, "count_tokens", lambda *_a, **_k: 10)
        flags: list[bool] = []

        def fake_run_cross_check(specs, _existing, **kwargs):
            flags.append(kwargs.get("chunk_subset", False))
            return _chunk_result("completed")

        monkeypatch.setattr(cc, "run_cross_check", fake_run_cross_check)
        specs = [_spec("22 11 00 - Water.docx"), _spec("23 05 00 - HVAC.docx")]
        run_chunked_cross_check(specs, [], cycle=DEFAULT_CYCLE)
        assert flags == [False]


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

    def test_combined_result_carries_chunk_failure_count(self, monkeypatch):
        # The failed chunk is counted on the combined result so the report
        # banner can flag the partially-incomplete pass (P1-3 follow-up).
        _force_chunking(monkeypatch)

        def fake_run_cross_check(specs, _existing, **_kwargs):
            filenames = {s.filename for s in specs}
            if any(f.startswith("22") for f in filenames):
                return _chunk_result("completed", findings=[_finding("22 11 00 - Water.docx")])
            return _chunk_result("failed", error="boom")

        monkeypatch.setattr(cc, "run_cross_check", fake_run_cross_check)
        specs = [
            _spec("22 11 00 - Water.docx"),
            _spec("22 13 00 - Sanitary.docx"),
            _spec("23 05 00 - HVAC.docx"),
            _spec("23 07 00 - Insulation.docx"),
        ]
        combined = run_chunked_cross_check(specs, [], cycle=DEFAULT_CYCLE)
        assert combined.cross_check_status == "completed"
        assert combined.chunk_failures == 1
        assert combined.chunk_skips == 0

    def test_combined_result_counts_skipped_chunks(self, monkeypatch):
        _force_chunking(monkeypatch)

        def fake_run_cross_check(specs, _existing, **_kwargs):
            filenames = {s.filename for s in specs}
            if any(f.startswith("22") for f in filenames):
                return _chunk_result("completed", findings=[_finding("22 11 00 - Water.docx")])
            return _chunk_result("skipped", thinking="division too large")

        monkeypatch.setattr(cc, "run_cross_check", fake_run_cross_check)
        specs = [
            _spec("22 11 00 - Water.docx"),
            _spec("22 13 00 - Sanitary.docx"),
            _spec("23 05 00 - HVAC.docx"),
            _spec("23 07 00 - Insulation.docx"),
        ]
        combined = run_chunked_cross_check(specs, [], cycle=DEFAULT_CYCLE)
        assert combined.cross_check_status == "completed"
        assert combined.chunk_skips == 1
        assert combined.chunk_failures == 0

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
        synthesis = synthesize_chunk_results(
            results, groups=_GROUPS, summary_title="Chunked cross-check"
        )
        assert synthesis.status == "completed"
        assert len(synthesis.findings) == 1

    def test_zero_completed_with_failures_is_failed(self):
        results = [
            ("div_22", _chunk_result("failed", error="x")),
            ("div_23", _chunk_result("failed", error="y")),
        ]
        synthesis = synthesize_chunk_results(
            results, groups=_GROUPS, summary_title="Chunked cross-check"
        )
        assert synthesis.status == "failed"
        assert synthesis.findings == []
        assert "0 completed, 2 failed" in synthesis.summary_text

    def test_zero_completed_only_skipped_is_skipped(self):
        results = [
            ("div_22", _chunk_result("skipped", thinking="too small")),
            ("div_23", _chunk_result("skipped", thinking="too small")),
        ]
        synthesis = synthesize_chunk_results(
            results, groups=_GROUPS, summary_title="Chunked cross-check"
        )
        assert synthesis.status == "skipped"


# ===========================================================================
# 6. All-chunks-failed carries the chunk errors on the combined result
# ===========================================================================


class TestAllChunksFailedCarriesError:
    SPECS = [
        "22 11 00 - Water.docx",
        "22 13 00 - Sanitary.docx",
        "23 05 00 - HVAC.docx",
        "23 07 00 - Insulation.docx",
    ]

    def _specs(self):
        return [_spec(name) for name in self.SPECS]

    def test_all_failed_names_every_chunk_error(self, monkeypatch):
        _force_chunking(monkeypatch)

        def fake_run_cross_check(specs, _existing, **_kwargs):
            filenames = {s.filename for s in specs}
            if any(f.startswith("22") for f in filenames):
                return _chunk_result("failed", error="div22: API 500 overloaded")
            return _chunk_result("failed", error="div23: connection reset")

        monkeypatch.setattr(cc, "run_cross_check", fake_run_cross_check)

        combined = run_chunked_cross_check(self._specs(), [], cycle=DEFAULT_CYCLE)

        assert combined.cross_check_status == "failed"
        assert combined.findings == []
        assert combined.chunk_failures == 2
        assert combined.error  # non-empty
        assert "div22: API 500 overloaded" in combined.error
        assert "div23: connection reset" in combined.error

    def test_all_failed_without_messages_gets_fallback_text(self, monkeypatch):
        _force_chunking(monkeypatch)
        monkeypatch.setattr(
            cc, "run_cross_check", lambda *_a, **_k: _chunk_result("failed", error="")
        )

        combined = run_chunked_cross_check(self._specs(), [], cycle=DEFAULT_CYCLE)

        assert combined.cross_check_status == "failed"
        assert combined.error == "All cross-check chunks failed."

    def test_partial_failure_leaves_error_none(self, monkeypatch):
        _force_chunking(monkeypatch)

        def fake_run_cross_check(specs, _existing, **_kwargs):
            filenames = {s.filename for s in specs}
            if any(f.startswith("22") for f in filenames):
                return _chunk_result("completed", findings=[_finding("22 11 00 - Water.docx")])
            return _chunk_result("failed", error="div23 boom")

        monkeypatch.setattr(cc, "run_cross_check", fake_run_cross_check)

        combined = run_chunked_cross_check(self._specs(), [], cycle=DEFAULT_CYCLE)

        assert combined.cross_check_status == "completed"
        assert combined.error is None
        assert combined.chunk_failures == 1  # telemetry still flags the failed chunk

    def test_pipeline_status_log_names_the_error(self, monkeypatch):
        # The operator-facing consequence: "Cross-check failed: <why>", never
        # "Cross-check failed: None".
        from src.orchestration.pipeline import _log_cross_check_status

        _force_chunking(monkeypatch)
        monkeypatch.setattr(
            cc, "run_cross_check", lambda *_a, **_k: _chunk_result("failed", error="quota exceeded")
        )
        combined = run_chunked_cross_check(self._specs(), [], cycle=DEFAULT_CYCLE)
        lines: list[str] = []
        _log_cross_check_status(lambda msg, **_kw: lines.append(msg), combined)

        assert lines == ["Cross-check failed: quota exceeded; quota exceeded"]
        assert "None" not in lines[0]
