"""Contract pins for the shared chunked-pass engine (``core.chunked_pass``).

Cross-check and compliance both fall back to one call per CSI chunk group
when a package exceeds the recommended input ceiling. They used to carry two
near-identical copies of the grouping / pooling / per-chunk loop / tally /
synthesis pipeline, and the copies drifted (the cross-check merge once forgot
to carry the chunk errors onto the combined result while compliance did).
:func:`~src.core.chunked_pass.run_chunked_pass` is now the single engine and
both ``run_chunked_*`` entry points are thin adapters over it.

These tests pin the engine directly — grouping completeness and pooling,
per-chunk scoping, labelling, the status matrix and summary text, partial
and total failure handling, telemetry, the hook order, and the one-permit-
per-call gate contract — so a future third chunked pass inherits every
guarantee for free, and pin that both adapters actually route through it.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import src.compliance.compliance_checker as compliance
import src.cross_check.cross_checker as cross
from src.core import chunked_pass as engine
from src.core.chunked_pass import (
    GENERAL_CHUNK_ID,
    GENERAL_CHUNK_LABEL,
    ChunkJob,
    assign_chunk,
    chunk_label,
    filter_findings_for_chunk,
    group_specs_by_chunk,
    label_finding_with_chunk,
    run_chunked_pass,
    synthesize_chunk_results,
)
from src.core.code_cycles import DEFAULT_CYCLE
from src.input.extractor import ExtractedSpec
from src.modules import ChunkGroup
from src.research import DimensionStatus, RequirementsProfile, ResearchItem
from src.review.reviewer import Finding, ReviewResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GROUPS = (
    ChunkGroup(chunk_id="div_21", label="Division 21 — Fire", csi_prefixes=("21",)),
    ChunkGroup(chunk_id="div_22", label="Division 22 — Plumbing", csi_prefixes=("22",)),
    ChunkGroup(chunk_id="controls", label="Controls + Cx", csi_prefixes=("25", "01")),
)


def _spec(filename: str, content: str = "Provide equipment per code.") -> ExtractedSpec:
    return ExtractedSpec(filename=filename, content=content, word_count=len(content.split()))


def _finding(filename: str, *, section: str = "2.1", issue: str = "coord", affected=()) -> Finding:
    f = Finding(
        severity="MEDIUM",
        fileName=filename,
        section=section,
        issue=issue,
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference="",
    )
    f.affected_files = list(affected)
    return f


def _result(status, *, findings=None, error=None, thinking="", coverage=None, tokens=(0, 0, 0, 0)) -> ReviewResult:
    r = ReviewResult(
        findings=findings or [],
        thinking=thinking,
        model="fake",
        cross_check_status=status,
        error=error,
        coverage=coverage or [],
    )
    r.input_tokens, r.output_tokens, r.cache_creation_input_tokens, r.cache_read_input_tokens = tokens
    return r


def _two_chunks() -> list[tuple[str, list[ExtractedSpec]]]:
    return group_specs_by_chunk(
        [
            _spec("21 13 13 Wet.docx"),
            _spec("21 13 16 Dry.docx"),
            _spec("22 11 13 Water.docx"),
            _spec("22 11 16 Piping.docx"),
        ],
        GROUPS,
    )


def _scripted_runner(by_chunk: dict[str, ReviewResult]):
    """A runner returning a scripted result per chunk id, recording its jobs."""
    jobs: list[ChunkJob] = []

    def run_chunk(job: ChunkJob) -> ReviewResult:
        jobs.append(job)
        return by_chunk[job.chunk_id]

    run_chunk.jobs = jobs  # type: ignore[attr-defined]
    return run_chunk


class CountingGate:
    """Non-reentrant context manager recording how it was used."""

    def __init__(self) -> None:
        self.acquisitions = 0
        self.held = False
        self.reentered = False

    def __enter__(self):
        if self.held:
            self.reentered = True
        self.held = True
        self.acquisitions += 1
        return self

    def __exit__(self, *_args):
        self.held = False
        return False


# ===========================================================================
# 1. Grouping: every spec in exactly one chunk, singleton pooling, order
# ===========================================================================


class TestGrouping:
    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("21 13 13 Wet.docx", "div_21"),
            ("22 11 13 Water.docx", "div_22"),
            ("25 90 00 Sequences.docx", "controls"),
            ("01 91 00 Commissioning.docx", "controls"),
            ("  23 05 00 leading-space.docx", GENERAL_CHUNK_ID),  # parseable, unclaimed
            ("Cover Sheet.docx", GENERAL_CHUNK_ID),               # no CSI prefix
            ("", GENERAL_CHUNK_ID),                                 # empty
        ],
    )
    def test_assign_chunk(self, filename, expected):
        assert assign_chunk(filename, GROUPS) == expected

    def test_chunk_label_for_groups_general_and_unknown(self):
        assert chunk_label("div_22", GROUPS) == "Division 22 — Plumbing"
        assert chunk_label(GENERAL_CHUNK_ID, GROUPS) == GENERAL_CHUNK_LABEL
        assert chunk_label("never_declared", GROUPS) == GENERAL_CHUNK_LABEL

    def test_every_spec_lands_in_exactly_one_chunk(self):
        specs = [
            _spec("22 11 13 Water.docx"),
            _spec("22 11 16 Piping.docx"),
            _spec("21 13 13 Wet.docx"),      # singleton division → pooled
            _spec("Cover.docx"),             # unparseable → general
            _spec("99 00 00 Unknown.docx"),  # unclaimed prefix → general
        ]
        chunks = group_specs_by_chunk(specs, GROUPS)
        grouped = [s for _cid, group in chunks for s in group]
        assert len(grouped) == len(specs), "a spec was dropped or duplicated"
        assert {s.filename for s in grouped} == {s.filename for s in specs}
        seen: set[str] = set()
        for _cid, group in chunks:
            for s in group:
                assert s.filename not in seen
                seen.add(s.filename)

    def test_singletons_pool_into_general_and_general_may_hold_one(self):
        chunks = dict(group_specs_by_chunk(
            [_spec("22 11 13 Water.docx"), _spec("22 11 16 Piping.docx"), _spec("21 13 13 Wet.docx")],
            GROUPS,
        ))
        assert "div_21" not in chunks
        assert [s.filename for s in chunks[GENERAL_CHUNK_ID]] == ["21 13 13 Wet.docx"]
        assert len(chunks["div_22"]) == 2

    def test_order_is_group_declaration_order_then_general_last(self):
        # Input deliberately reversed relative to the group order.
        specs = [
            _spec("Cover.docx"),
            _spec("25 90 00 A.docx"), _spec("01 91 00 B.docx"),
            _spec("22 11 13 A.docx"), _spec("22 11 16 B.docx"),
            _spec("21 13 13 A.docx"), _spec("21 13 16 B.docx"),
        ]
        assert [cid for cid, _ in group_specs_by_chunk(specs, GROUPS)] == [
            "div_21", "div_22", "controls", GENERAL_CHUNK_ID,
        ]

    def test_empty_input_yields_no_chunks(self):
        assert group_specs_by_chunk([], GROUPS) == []


# ===========================================================================
# 2. Per-chunk scoping + labelling
# ===========================================================================


class TestScopingAndLabelling:
    def test_filter_keeps_findings_by_filename_or_affected_files(self):
        inside = _finding("22 11 13 Water.docx")
        via_affected = _finding("99 00 00 Other.docx", affected=("22 11 16 Piping.docx",))
        outside = _finding("21 13 13 Wet.docx")
        kept = filter_findings_for_chunk(
            [inside, via_affected, outside], {"22 11 13 Water.docx", "22 11 16 Piping.docx"}
        )
        assert kept == [inside, via_affected]

    def test_filter_with_no_filenames_returns_a_copy_of_everything(self):
        existing = [_finding("a.docx"), _finding("b.docx")]
        kept = filter_findings_for_chunk(existing, set())
        assert kept == existing
        assert kept is not existing

    def test_label_prefixes_section_and_is_idempotent(self):
        f = _finding("22 11 13 Water.docx", section="2.1:")
        label_finding_with_chunk(f, "div_22", GROUPS)
        assert f.section == "[Division 22 — Plumbing] 2.1"
        label_finding_with_chunk(f, "div_22", GROUPS)  # label already present
        assert f.section == "[Division 22 — Plumbing] 2.1"

    def test_label_on_empty_section_is_the_bare_label(self):
        f = _finding("Cover.docx", section="")
        label_finding_with_chunk(f, GENERAL_CHUNK_ID, GROUPS)
        assert f.section == f"[{GENERAL_CHUNK_LABEL}]"


# ===========================================================================
# 3. Synthesis: status matrix + summary text
# ===========================================================================


class TestSynthesis:
    @pytest.mark.parametrize(
        "statuses,expected",
        [
            (["completed", "failed"], "completed"),
            (["completed", "skipped"], "completed"),
            (["failed", "failed"], "failed"),
            (["failed", "skipped"], "failed"),
            (["skipped", "skipped"], "skipped"),
            ([None], "failed"),  # anything neither completed nor skipped is a failure
            ([], "skipped"),     # nothing ran
        ],
    )
    def test_status_matrix(self, statuses, expected):
        results = [(f"c{i}", _result(s)) for i, s in enumerate(statuses)]
        assert synthesize_chunk_results(results, groups=GROUPS, summary_title="T").status == expected

    def test_header_and_per_chunk_sections_exact(self):
        results = [
            ("div_21", _result("completed", thinking="  Fire OK.  ", findings=[_finding("21 13 13 Wet.docx")])),
            ("div_22", _result("failed", error="API 500")),
            (GENERAL_CHUNK_ID, _result("skipped", thinking="too small")),
        ]
        s = synthesize_chunk_results(results, groups=GROUPS, summary_title="Chunked cross-check")
        assert s.summary_text == (
            "Chunked cross-check (1 completed, 1 failed, 1 skipped). Per-chunk summaries follow.\n"
            "--- Division 21 — Fire ---\nFire OK.\n\n"
            "--- Division 22 — Plumbing ---\nFailed: API 500\n\n"
            f"--- {GENERAL_CHUNK_LABEL} ---\nSkipped: too small"
        )
        assert (s.completed, s.failed, s.skipped) == (1, 1, 1)
        assert [f.section for f in s.findings] == ["[Division 21 — Fire] 2.1"]

    def test_fallback_texts_for_missing_reason_and_error(self):
        results = [("div_21", _result("skipped")), ("div_22", _result("failed"))]
        s = synthesize_chunk_results(results, groups=GROUPS, summary_title="T")
        assert "Skipped: no reason given" in s.summary_text
        assert "Failed: unknown error" in s.summary_text

    def test_completed_chunk_without_thinking_adds_no_section_and_header_only_has_no_join(self):
        s = synthesize_chunk_results([("div_21", _result("completed"))], groups=GROUPS, summary_title="T")
        assert s.summary_text == "T (1 completed, 0 failed, 0 skipped). Per-chunk summaries follow.\n"

    def test_summary_heading_override_changes_headings_but_not_finding_labels(self):
        results = [("div_21", _result("completed", thinking="x", findings=[_finding("21 13 13 Wet.docx")]))]
        s = synthesize_chunk_results(
            results, groups=GROUPS, summary_title="T", summary_heading=lambda chunk_id: chunk_id
        )
        assert "--- div_21 ---" in s.summary_text
        assert "Division 21 — Fire ---" not in s.summary_text
        # Findings are always labelled with the human-readable group label.
        assert s.findings[0].section == "[Division 21 — Fire] 2.1"

    def test_failed_chunk_findings_are_never_carried(self):
        results = [("div_21", _result("failed", findings=[_finding("21 13 13 Wet.docx")]))]
        assert synthesize_chunk_results(results, groups=GROUPS, summary_title="T").findings == []


# ===========================================================================
# 4. run_chunked_pass: loop, partial/total failure, telemetry, hooks, gate
# ===========================================================================


class TestRunChunkedPass:
    def test_runner_called_once_per_chunk_in_order_with_scoped_findings(self):
        chunks = _two_chunks()
        existing = [
            _finding("21 13 13 Wet.docx", issue="fire"),
            _finding("22 11 13 Water.docx", issue="water"),
            _finding("Cover.docx", issue="elsewhere"),
        ]
        runner = _scripted_runner({"div_21": _result("completed"), "div_22": _result("completed")})

        run_chunked_pass(chunks, existing, groups=GROUPS, run_chunk=runner,
                         pass_name="p", summary_title="T", model="m")

        jobs = runner.jobs
        assert [j.chunk_id for j in jobs] == ["div_21", "div_22"]
        assert [j.label for j in jobs] == ["Division 21 — Fire", "Division 22 — Plumbing"]
        assert jobs[0].specs is chunks[0][1] and jobs[1].specs is chunks[1][1]
        assert [f.issue for f in jobs[0].existing_findings] == ["fire"]
        assert [f.issue for f in jobs[1].existing_findings] == ["water"]

    def test_partial_failure_preserves_findings_and_telemetry(self):
        runner = _scripted_runner({
            "div_21": _result("completed", findings=[_finding("21 13 13 Wet.docx")], thinking="ok"),
            "div_22": _result("failed", error="boom"),
        })
        combined = run_chunked_pass(_two_chunks(), [], groups=GROUPS, run_chunk=runner,
                                    pass_name="p", summary_title="Chunked p", model="m")
        assert combined.cross_check_status == "completed"
        assert [f.section for f in combined.findings] == ["[Division 21 — Fire] 2.1"]
        assert (combined.chunk_failures, combined.chunk_skips) == (1, 0)
        assert combined.error is None
        assert combined.thinking.startswith("Chunked p (1 completed, 1 failed, 0 skipped).")
        assert combined.model == "m"

    def test_total_failure_joins_every_chunk_error(self):
        runner = _scripted_runner({
            "div_21": _result("failed", error="div21: overloaded"),
            "div_22": _result("failed", error="div22: reset"),
        })
        combined = run_chunked_pass(_two_chunks(), [], groups=GROUPS, run_chunk=runner,
                                    pass_name="cross-check", summary_title="T", model="m")
        assert combined.cross_check_status == "failed"
        assert combined.error == "div21: overloaded; div22: reset"
        assert combined.findings == []
        assert combined.chunk_failures == 2

    def test_total_failure_without_messages_uses_pass_name_fallback(self):
        runner = _scripted_runner({"div_21": _result("failed"), "div_22": _result("failed", error="")})
        combined = run_chunked_pass(_two_chunks(), [], groups=GROUPS, run_chunk=runner,
                                    pass_name="compliance", summary_title="T", model="m")
        assert combined.error == "All compliance chunks failed."

    def test_total_skip_is_skipped_with_no_error(self):
        runner = _scripted_runner({"div_21": _result("skipped", thinking="a"), "div_22": _result("skipped")})
        combined = run_chunked_pass(_two_chunks(), [], groups=GROUPS, run_chunk=runner,
                                    pass_name="p", summary_title="T", model="m")
        assert combined.cross_check_status == "skipped"
        assert (combined.chunk_failures, combined.chunk_skips) == (0, 2)
        assert combined.error is None

    def test_token_counters_sum_over_every_chunk_including_failed(self):
        runner = _scripted_runner({
            "div_21": _result("completed", tokens=(100, 10, 5, 50)),
            "div_22": _result("failed", error="x", tokens=(7, 3, 1, 2)),
        })
        combined = run_chunked_pass(_two_chunks(), [], groups=GROUPS, run_chunk=runner,
                                    pass_name="p", summary_title="T", model="m")
        assert (combined.input_tokens, combined.output_tokens) == (107, 13)
        assert (combined.cache_creation_input_tokens, combined.cache_read_input_tokens) == (6, 52)
        assert combined.elapsed_seconds >= 0.0

    def test_hooks_merge_completed_coverage_then_filter_labelled_findings(self):
        cov21 = [{"requirement_id": "r-1", "status": "represented"}]
        cov_failed = [{"requirement_id": "r-9", "status": "missing"}]
        runner = _scripted_runner({
            "div_21": _result("completed", coverage=cov21, findings=[_finding("21 13 13 Wet.docx")]),
            "div_22": _result("failed", error="x", coverage=cov_failed),
        })
        seen: dict = {}

        def coverage_merge(lists):
            seen["merge_input"] = lists
            return [{"requirement_id": "r-1", "status": "merged"}]

        def finding_filter(findings, coverage):
            seen["filter_input"] = (list(findings), coverage)
            return []

        combined = run_chunked_pass(_two_chunks(), [], groups=GROUPS, run_chunk=runner,
                                    pass_name="p", summary_title="T", model="m",
                                    coverage_merge=coverage_merge, finding_filter=finding_filter)
        # Only the completed chunk's coverage reaches the merge.
        assert seen["merge_input"] == [cov21]
        # The filter sees already-labelled findings plus the merged coverage.
        labelled, merged = seen["filter_input"]
        assert [f.section for f in labelled] == ["[Division 21 — Fire] 2.1"]
        assert merged == [{"requirement_id": "r-1", "status": "merged"}]
        # ... and its answer is what lands on the result.
        assert combined.findings == []
        assert combined.coverage == merged

    def test_without_hooks_findings_pass_through_and_coverage_is_empty(self):
        runner = _scripted_runner({
            "div_21": _result("completed", coverage=[{"requirement_id": "r-1", "status": "missing"}],
                              findings=[_finding("21 13 13 Wet.docx")]),
            "div_22": _result("completed"),
        })
        combined = run_chunked_pass(_two_chunks(), [], groups=GROUPS, run_chunk=runner,
                                    pass_name="p", summary_title="T", model="m")
        assert len(combined.findings) == 1
        assert combined.coverage == []

    def test_gate_is_taken_once_per_call_and_never_by_the_engine(self):
        gate = CountingGate()
        held_between_calls: list[bool] = []

        def run_chunk(job: ChunkJob) -> ReviewResult:
            held_between_calls.append(gate.held)  # engine adds no outer hold
            with gate:
                pass  # one permit around exactly one API call
            return _result("completed")

        chunks = _two_chunks()
        run_chunked_pass(chunks, [], groups=GROUPS, run_chunk=run_chunk,
                         pass_name="p", summary_title="T", model="m")
        assert gate.acquisitions == len(chunks) == 2
        assert held_between_calls == [False, False]
        assert gate.held is False and gate.reentered is False
        assert "call_gate" not in inspect.signature(run_chunked_pass).parameters

    def test_runner_exceptions_propagate(self):
        # Runners own failure-to-result conversion (they never raise on API
        # errors); the engine does not mask a contract violation.
        def run_chunk(_job):
            raise RuntimeError("runner broke its contract")

        with pytest.raises(RuntimeError, match="contract"):
            run_chunked_pass(_two_chunks(), [], groups=GROUPS, run_chunk=run_chunk,
                             pass_name="p", summary_title="T", model="m")


# ===========================================================================
# 5. Both adapters route through the engine (no re-inlined merge)
# ===========================================================================


def _spy_engine(monkeypatch, module):
    seen: dict = {}
    sentinel = ReviewResult(findings=[], cross_check_status="completed", thinking="from engine")

    def spy(chunks, existing, **kwargs):
        seen["chunks"] = chunks
        seen["existing"] = existing
        seen.update(kwargs)
        return sentinel

    monkeypatch.setattr(module, "run_chunked_pass", spy)
    return seen, sentinel


class TestAdaptersDriveTheEngine:
    def test_cross_check_adapter(self, monkeypatch):
        monkeypatch.setattr(cross, "count_tokens", lambda *_a, **_k: cross.CROSS_CHECK_RECOMMENDED_MAX)
        seen, sentinel = _spy_engine(monkeypatch, cross)
        forwarded: dict = {}
        monkeypatch.setattr(
            cross, "run_cross_check",
            lambda specs, existing, **kw: forwarded.update(specs=specs, existing=existing, **kw) or _result("completed"),
        )
        gate = CountingGate()
        specs = [_spec("21 13 13 Wet.docx"), _spec("21 13 16 Dry.docx"),
                 _spec("22 11 13 Water.docx"), _spec("22 11 16 Piping.docx")]
        existing = [_finding("22 11 13 Water.docx")]

        out = cross.run_chunked_cross_check(specs, existing, cycle=DEFAULT_CYCLE, model="m", call_gate=gate)

        assert out is sentinel
        assert seen["pass_name"] == "cross-check"
        assert seen["summary_title"] == "Chunked cross-check"
        assert seen["model"] == "m"
        assert seen["existing"] is existing
        assert seen.get("summary_heading") is None
        assert seen.get("coverage_merge") is None and seen.get("finding_filter") is None
        assert [cid for cid, _ in seen["chunks"]] == ["div_21", "div_22"]
        # The adapter's runner forwards the chunk's specs and scoped findings
        # to the un-chunked pass with the per-call gate.
        job = ChunkJob(chunk_id="div_22", label="L", specs=specs[2:], existing_findings=existing)
        seen["run_chunk"](job)
        assert forwarded["specs"] is job.specs
        assert forwarded["existing"] is job.existing_findings
        assert forwarded["call_gate"] is gate
        assert forwarded["cycle"] is DEFAULT_CYCLE and forwarded["model"] == "m"
        assert "_trace_parent" in forwarded

    def test_compliance_adapter(self, monkeypatch):
        monkeypatch.setattr(compliance, "count_tokens", lambda text: len(text.split()))
        monkeypatch.setattr(compliance, "COMPLIANCE_RECOMMENDED_MAX", 500)
        seen, sentinel = _spy_engine(monkeypatch, compliance)
        forwarded: dict = {}
        monkeypatch.setattr(
            compliance, "run_compliance_check",
            lambda specs, profile, existing, **kw: forwarded.update(specs=specs, profile=profile, existing=existing, **kw) or _result("completed"),
        )
        profile = RequirementsProfile(
            items=[ResearchItem(item_id="r-aaaaaaaaaaaa", dimension_id="d", topic="t", category="governing_code",
                                requirement="Adopted edition applies.", grounded=True,
                                accepted_sources=["https://codes.example.gov/x"], confidence=0.8)],
            dimension_statuses=[DimensionStatus(dimension_id="d", status="completed")],
            research_date="2026-07-14",
            project={"city": "Markham", "state_or_province": "ON", "country": "CA", "client_name": "ExampleCo"},
        )
        pad = "word " * 400
        specs = [_spec("21 13 13 Wet.docx", pad), _spec("21 13 16 Dry.docx", pad),
                 _spec("22 11 13 Water.docx", pad), _spec("22 11 16 Piping.docx", pad)]
        gate = CountingGate()

        out = compliance.run_chunked_compliance_check(specs, profile, [], cycle=DEFAULT_CYCLE, model="m", call_gate=gate)

        assert out is sentinel
        assert seen["pass_name"] == "compliance"
        assert seen["summary_title"] == "Chunked compliance check"
        assert seen["model"] == "m"
        assert seen["summary_heading"]("div_21") == "div_21"  # compliance heads sections by chunk id
        assert seen["coverage_merge"] is compliance._merge_coverage_lists
        assert seen["finding_filter"] is compliance._filter_chunk_findings
        assert [cid for cid, _ in seen["chunks"]] == ["div_21", "div_22"]
        job = ChunkJob(chunk_id="div_22", label="L", specs=specs[2:], existing_findings=[])
        seen["run_chunk"](job)
        assert forwarded["specs"] is job.specs and forwarded["profile"] is profile
        assert forwarded["chunk_subset"] is True
        assert forwarded["call_gate"] is gate
        assert "_trace_parent" in forwarded


# ===========================================================================
# 6. Layering: the engine never imports the passes it serves
# ===========================================================================


def test_engine_imports_none_of_the_passes_or_orchestration():
    """The engine's only first-party runtime import is the shared result type.

    ``core`` must stay importable by both passes without a cycle, so the
    engine may not import ``cross_check`` / ``compliance`` / ``orchestration``
    (nor ``modules`` / ``input`` — those are annotation-only, under
    ``TYPE_CHECKING``).
    """
    source = Path(engine.__file__).read_text(encoding="utf-8")
    first_party = [
        node.module
        for node in ast.parse(source).body  # top level only; TYPE_CHECKING is an ``if``
        if isinstance(node, ast.ImportFrom) and node.level > 0
    ]
    assert first_party == ["review.reviewer"], first_party
