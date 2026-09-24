"""Finding identity keeps different issues apart (plan WP-06A, chunk S02).

The dedup key — and the ``rf-`` / ``cf-`` / ``lc-`` id minted from it —
used to delete everything from a CSI-shaped number through the next
``.docx``. "Section 21 05 00 requires copper pipe in 210500.docx." and the
PVC version of the same sentence both became "section .", so
``_deduplicate_findings`` merged them and one requirement vanished from the
report and the edit sidecar.

Identity now disregards only *exact file names the run knows about*
(:class:`FindingIdentityContext`), as whole tokens, and keeps every other
word. These tests pin, in order:

* the plan's Appendix A example, both directions: copper and PVC stay apart,
  while the same issue reported against different known files still groups;
* that only known names are removed, and nothing is guessed without a corpus;
* the matching rules — case, whitespace, literal punctuation, token
  boundaries, and overlapping names (longest first);
* order independence of the context and of the grouping;
* that review, cross-check, and compliance use one context, derived from the
  submission, through the real stage functions — plus an AST tripwire so a
  new production call site cannot quietly fall back to the empty context;
* that ids move only where the old key was wrong (the release-note claim).

Everything is hermetic: the cross-check and compliance passes are stubbed.
"""
from __future__ import annotations

import ast
import dataclasses
import hashlib
import itertools
import re
import time
from pathlib import Path

import pytest

from src.batch.batch import BatchJob
from src.input.extractor import ExtractedSpec
from src.orchestration import pipeline
from src.orchestration.pipeline import (
    EMPTY_FINDING_IDENTITY_CONTEXT,
    BatchSubmission,
    CollectedBatchState,
    FindingIdentityContext,
    compute_finding_id,
    finding_identity_context_for_submission,
)
from src.review.reviewer import Finding, ReviewResult

_SRC = Path(__file__).resolve().parents[1] / "src"

COPPER = "Section 21 05 00 requires copper pipe in 210500.docx."
PVC = "Section 21 05 00 requires PVC pipe in 210500.docx."


def _finding(issue: str, *, file: str = "210500.docx", **overrides) -> Finding:
    fields = dict(
        severity="HIGH",
        fileName=file,
        section="2.01",
        issue=issue,
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference=None,
    )
    fields.update(overrides)
    return Finding(**fields)


def _context(*names: str) -> FindingIdentityContext:
    return FindingIdentityContext.from_filenames(names)


def _groups(findings: list[Finding], context: FindingIdentityContext) -> set:
    """Order-free summary of a dedup result: (id, files) per group."""
    merged = pipeline._deduplicate_findings(findings, context=context)
    return {(f.finding_id, tuple(sorted(f.affected_files))) for f in merged}


# ---------------------------------------------------------------------------
# Plan Appendix A — both directions
# ---------------------------------------------------------------------------


class TestAppendixAExample:
    def test_copper_and_pvc_in_a_known_file_stay_distinct(self):
        context = _context("210500.docx")
        merged = pipeline._deduplicate_findings(
            [_finding(COPPER), _finding(PVC)], context=context
        )
        assert [f.issue for f in merged] == [COPPER, PVC]
        assert len({f.finding_id for f in merged}) == 2
        assert context.normalize_issue_text(COPPER) == (
            "section 21 05 00 requires copper pipe in ."
        )

    def test_the_same_issue_in_different_known_files_still_groups(self):
        context = _context("210500.docx", "210510.docx")
        a = _finding(COPPER)
        b = _finding(
            "Section 21 05 00 requires copper pipe in 210510.docx.", file="210510.docx"
        )
        (merged,) = pipeline._deduplicate_findings([a, b], context=context)
        assert merged.affected_files == ["210500.docx", "210510.docx"]
        assert merged.finding_id == compute_finding_id(a, context=context)
        assert merged.finding_id == compute_finding_id(b, context=context)
        # Each file keeps its own pre-merge original, issue text included.
        assert [o.issue for o in merged.occurrence_originals] == [a.issue, b.issue]

    def test_grouping_still_requires_every_other_key_field_to_agree(self):
        context = _context("210500.docx", "210510.docx")
        a = _finding(COPPER)
        b = _finding(
            "Section 21 05 00 requires copper pipe in 210510.docx.",
            file="210510.docx",
            section="2.02",
        )
        assert len(pipeline._deduplicate_findings([a, b], context=context)) == 2


# ---------------------------------------------------------------------------
# Only known names are removed; nothing is guessed without a corpus
# ---------------------------------------------------------------------------


class TestOnlyKnownNamesAreRemoved:
    def test_without_a_corpus_the_text_is_kept(self):
        text = "  Section 21 05 00\trequires copper pipe in\n210500.docx. "
        assert EMPTY_FINDING_IDENTITY_CONTEXT.normalize_issue_text(text) == (
            "section 21 05 00 requires copper pipe in 210500.docx."
        )
        assert pipeline._normalize_issue_text(text) == (
            "section 21 05 00 requires copper pipe in 210500.docx."
        )

    def test_an_unknown_name_is_kept(self):
        context = _context("210500.docx")
        assert context.normalize_issue_text("Coordinate with 999999.docx.") == (
            "coordinate with 999999.docx."
        )

    def test_the_old_generic_shape_is_not_stripped_when_unknown(self):
        """The CSI-number-through-.docx span the old rule deleted is prose
        unless its file name is in the corpus."""
        context = _context("210500.docx")
        text = "Section 21 05 00 requires copper pipe in 21 05 99 - Other.docx"
        assert context.normalize_issue_text(text) == text.lower()

    def test_findings_naming_different_unknown_files_stay_apart(self):
        context = _context("210500.docx")
        merged = pipeline._deduplicate_findings(
            [
                _finding("Coordinate with 999998.docx."),
                _finding("Coordinate with 999999.docx."),
            ],
            context=context,
        )
        assert len(merged) == 2

    def test_empty_and_blank_names_are_not_part_of_the_corpus(self):
        context = FindingIdentityContext.from_filenames(["", None, "   ", "a.docx"])
        assert context.known_filenames == ("a.docx",)
        assert FindingIdentityContext.from_filenames([None, ""]) == (
            EMPTY_FINDING_IDENTITY_CONTEXT
        )


# ---------------------------------------------------------------------------
# Matching rules
# ---------------------------------------------------------------------------


class TestNameMatching:
    @pytest.mark.parametrize(
        "known, text, expected",
        [
            # Extension case, either side.
            ("210500.DOCX", "Issue in 210500.docx.", "issue in ."),
            ("210500.docx", "Issue in 210500.DOCX.", "issue in ."),
            ("Überdruck.docx", "Issue in ÜBERDRUCK.DOCX here", "issue in here"),
            # Whitespace inside a name, collapsed on both sides.
            (
                "21 05 00 - Common Work Results.docx",
                "Issue in 21  05 00 -\nCommon\tWork Results.docx here",
                "issue in here",
            ),
        ],
    )
    def test_case_and_whitespace_are_normalized(self, known, text, expected):
        assert _context(known).normalize_issue_text(text) == expected

    @pytest.mark.parametrize(
        "known",
        [
            "Fire (Rev. 2).docx",
            "Spec [draft] +1.docx",
            "Pump $ 2^3 {a|b}*?.docx",
            "back\\slash.docx",
        ],
    )
    def test_names_are_matched_literally(self, known):
        text = f"Issue in {known} here."
        assert _context(known).normalize_issue_text(text) == "issue in here."

    def test_a_dot_in_a_name_is_not_a_wildcard(self):
        context = _context("a.docx")
        assert context.normalize_issue_text("See aXdocx and a.docx.") == "see axdocx and ."

    @pytest.mark.parametrize(
        "text",
        [
            'Issue in "210500.docx".',
            "Issue in (210500.docx).",
            "Issue in 210500.docx, then more.",
            "Issue in 210500.docx: detail.",
            "Issue in 210500.docx's PART 2.",
            "Issue in specs/210500.docx.",
            "Issue in C:\\specs\\210500.docx.",
            "210500.docx",
        ],
    )
    def test_prose_punctuation_may_touch_a_name(self, text):
        assert "210500.docx" not in _context("210500.docx").normalize_issue_text(text)

    @pytest.mark.parametrize(
        "text",
        [
            "Issue in 1210500.docx.",
            "Issue in a-210500.docx.",
            "Issue in a_210500.docx.",
            "Issue in backup.210500.docx.",
            "Issue in 210500.docx.bak.",
            "Issue in 210500.docxx.",
            "Issue in 210500.docx-old.",
        ],
    )
    def test_a_name_glued_into_a_longer_name_is_kept(self, text):
        assert _context("210500.docx").normalize_issue_text(text) == text.lower()

    def test_overlapping_names_are_removed_longest_first(self):
        context = _context("0500.docx", "210500.docx")
        assert context.normalize_issue_text("A 210500.docx and 0500.docx.") == "a and ."
        spaced = _context("Results.docx", "Common Work Results.docx")
        assert spaced.normalize_issue_text("See Common Work Results.docx now") == "see now"
        assert spaced.normalize_issue_text("See Results.docx now") == "see now"

    def test_a_known_name_is_never_half_removed_from_a_longer_known_name(self):
        context = _context("0500.docx", "210500.docx")
        for name in ("210500.docx", "0500.docx"):
            normalized = context.normalize_issue_text(f"x {name} y")
            assert normalized == "x y", name

    @pytest.mark.parametrize("order", ["short-first", "long-first"])
    def test_when_one_name_starts_another_the_longer_one_wins(self, order):
        """The only case where alternation order decides the match: both
        names start at the same position."""
        names = ["spec.docx", "spec.docx - Copy.docx"]
        if order == "long-first":
            names.reverse()
        context = _context(*names)
        assert context.normalize_issue_text("See spec.docx - Copy.docx now") == "see now"
        assert context.normalize_issue_text("See spec.docx now") == "see now"

    def test_every_occurrence_is_removed(self):
        context = _context("a.docx", "b.docx")
        assert context.normalize_issue_text(
            "Conflict between a.docx and b.docx; see a.docx."
        ) == "conflict between and ; see ."


# ---------------------------------------------------------------------------
# Order independence
# ---------------------------------------------------------------------------

_NAMES = ["210500.docx", "0500.docx", "21 05 00 - Common.docx", "210510.DOCX"]


class TestOrderIndependence:
    def test_the_context_does_not_depend_on_name_order(self):
        contexts = {
            _context(*order) for order in itertools.permutations(_NAMES)
        }
        assert len(contexts) == 1
        (context,) = contexts
        text = "In 210500.docx, 0500.docx, 21 05 00 - Common.docx and 210510.docx."
        for order in itertools.permutations(_NAMES):
            assert _context(*order).normalize_issue_text(text) == (
                context.normalize_issue_text(text)
            )

    def test_grouping_and_ids_do_not_depend_on_input_order(self):
        context = _context("210500.docx", "210510.docx", "210520.docx")
        findings = [
            _finding(COPPER),
            _finding(PVC),
            _finding(
                "Section 21 05 00 requires copper pipe in 210510.docx.",
                file="210510.docx",
            ),
            _finding(
                "Section 21 05 00 requires PVC pipe in 210520.docx.", file="210520.docx"
            ),
            _finding("Coordinate with 999999.docx.", file="210520.docx"),
        ]
        expected = None
        for order in itertools.permutations(range(len(findings))):
            fresh = [dataclasses.replace(findings[i]) for i in order]
            summary = _groups(fresh, context)
            expected = expected or summary
            assert summary == expected
        assert {files for _id, files in expected} == {
            ("210500.docx", "210510.docx"),
            ("210500.docx", "210520.docx"),
            ("210520.docx",),
        }


# ---------------------------------------------------------------------------
# One context for review, cross-check, and compliance
# ---------------------------------------------------------------------------


def _spec(name: str) -> ExtractedSpec:
    content = "PART 1 GENERAL\n\nProvide pipe."
    return ExtractedSpec(filename=name, content=content, word_count=4)


def _requirements_profile() -> dict:
    from src.research import DimensionStatus, RequirementsProfile, ResearchItem

    item = ResearchItem(
        item_id="r-aaaaaaaaaaaa",
        dimension_id="governing_codes",
        topic="Topic",
        category="governing_code",
        requirement="The 2024 IBC as amended governs.",
        grounded=True,
        accepted_sources=["https://codes.example.gov/x"],
        confidence=0.8,
    )
    return RequirementsProfile(
        items=[item],
        dimension_statuses=[
            DimensionStatus(dimension_id="governing_codes", status="completed")
        ],
        research_date="2026-07-14",
        project={"city": "Ashburn", "state_or_province": "VA", "country": "US"},
    ).to_dict()


def _submission(**overrides) -> BatchSubmission:
    files = ["210500.docx", "210510.docx"]
    request_map = {
        f"review__{i}": {"filename": name, "index": i, "type": "review"}
        for i, name in enumerate(files)
    }
    fields = dict(
        job=BatchJob(
            batch_id="realtime",
            job_type="review",
            request_map=request_map,
            created_at=time.time(),
        ),
        files_reviewed=list(files),
        review_request_ids=list(request_map),
        prepared_specs=[_spec(name) for name in files],
        review_transport="realtime",
        realtime_results={},
        cross_check_enabled=True,
    )
    fields.update(overrides)
    return BatchSubmission(**fields)


class TestOneContextForEveryStage:
    def test_the_submission_context_reads_every_source_of_names(self):
        submission = _submission(
            files_reviewed=["a.docx"],
            prepared_specs=[_spec("c.docx")],
            job=BatchJob(
                batch_id="b",
                job_type="review",
                request_map={
                    "r0": {"filename": "b.docx", "index": 0, "type": "review"},
                    "unrelated": {"filename": "z.docx", "index": 9, "type": "review"},
                },
                created_at=time.time(),
            ),
            review_request_ids=["r0"],
        )
        context = finding_identity_context_for_submission(submission)
        assert set(context.known_filenames) == {"a.docx", "b.docx", "c.docx"}

    def test_review_collection_groups_with_the_run_corpus(self):
        copper_b = _finding(
            "Section 21 05 00 requires copper pipe in 210510.docx.", file="210510.docx"
        )
        submission = _submission(
            realtime_results={
                "review__0": ReviewResult(findings=[_finding(COPPER), _finding(PVC)]),
                "review__1": ReviewResult(findings=[copper_b]),
            }
        )
        context = finding_identity_context_for_submission(submission)
        state = pipeline.collect_review_batch_results(submission)
        findings = state.review_result.findings
        assert len(findings) == 2  # copper (two files) + PVC
        by_files = {tuple(f.affected_files): f for f in findings}
        copper = by_files[("210500.docx", "210510.docx")]
        pvc = by_files[("210500.docx",)]
        assert copper.finding_id == compute_finding_id(_finding(COPPER), context=context)
        assert pvc.finding_id == compute_finding_id(_finding(PVC), context=context)
        assert copper.finding_id != pvc.finding_id

    def _collected(self, submission) -> CollectedBatchState:
        return CollectedBatchState(submission=submission, review_result=ReviewResult())

    def test_cross_check_ids_use_the_submission_context_not_the_specs_passed(
        self, monkeypatch
    ):
        coordination = _finding("Pipe material conflicts with 210510.docx.")
        monkeypatch.setattr(
            pipeline,
            "run_chunked_cross_check",
            lambda *_a, **_k: ReviewResult(
                findings=[coordination], cross_check_status="completed"
            ),
        )
        submission = _submission()
        state = pipeline.run_cross_check_for_batch(
            self._collected(submission),
            # A filtered subset that does not include 210510.docx: the
            # context must still come from the submission.
            specs=[_spec("210500.docx")],
        )
        (stamped,) = state.cross_check_result.findings
        context = finding_identity_context_for_submission(submission)
        probe = _finding("Pipe material conflicts with 210510.docx.")
        assert stamped.finding_id == compute_finding_id(probe, prefix="cf", context=context)
        assert stamped.finding_id != compute_finding_id(probe, prefix="cf")

    def test_compliance_ids_use_the_submission_context(self, monkeypatch):
        import src.compliance as compliance_pkg
        from src.modules.registry import get_module

        module = get_module("datacenter_fire")
        assert module.project_profile_enabled  # precondition: the pass runs
        finding = _finding("Missing requirement r-aaaaaaaaaaaa in 210510.docx.")
        monkeypatch.setattr(
            compliance_pkg,
            "run_chunked_compliance_check",
            lambda *_a, **_k: ReviewResult(
                findings=[finding], cross_check_status="completed"
            ),
        )
        submission = _submission(
            module_id=module.module_id,
            cycle_label=module.cycle.label,
            requirements_profile=_requirements_profile(),
        )
        state = pipeline.run_compliance_for_batch(self._collected(submission))
        (stamped,) = state.compliance_result.findings
        context = finding_identity_context_for_submission(submission)
        probe = _finding(
            "Missing requirement r-aaaaaaaaaaaa in 210510.docx.",
            section=stamped.section,
        )
        assert stamped.finding_id == compute_finding_id(probe, prefix="lc", context=context)
        assert stamped.finding_id != compute_finding_id(probe, prefix="lc")

    def test_the_origin_prefixes_are_kept(self):
        context = _context("210500.docx")
        ids = {
            compute_finding_id(_finding(COPPER), prefix=prefix, context=context)
            for prefix in ("rf", "cf", "lc")
        }
        assert {i.split("-", 1)[0] for i in ids} == {"rf", "cf", "lc"}
        assert len({i.split("-", 1)[1] for i in ids}) == 1


# ---------------------------------------------------------------------------
# No global mutable state
# ---------------------------------------------------------------------------


class TestNoGlobalState:
    def test_the_context_is_immutable(self):
        context = _context("210500.docx")
        with pytest.raises(dataclasses.FrozenInstanceError):
            context.known_filenames = ("other.docx",)  # type: ignore[misc]

    def test_a_corpus_used_elsewhere_never_leaks_into_the_default(self):
        before = compute_finding_id(_finding(COPPER))
        with_corpus = compute_finding_id(_finding(COPPER), context=_context("210500.docx"))
        assert pipeline._normalize_issue_text(COPPER) == COPPER.lower()
        assert compute_finding_id(_finding(COPPER)) == before != with_corpus


# ---------------------------------------------------------------------------
# Every production call site passes the run's context (AST tripwire)
# ---------------------------------------------------------------------------

_KEYWORD_CONTEXT_CALLS = {
    "_deduplicate_findings",
    "compute_finding_id",
    "assign_cross_check_finding_ids",
    "assign_compliance_finding_ids",
}
_POSITIONAL_CONTEXT_CALLS = {"_dedup_key": 2, "_normalize_issue_text": 2}
_IDENTITY_FUNCTIONS = _KEYWORD_CONTEXT_CALLS | set(_POSITIONAL_CONTEXT_CALLS)


_PIPELINE = _SRC / "orchestration" / "pipeline.py"


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _pipeline_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Names imported from the pipeline module, and aliases of the module."""
    names: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            source = node.module or ""
            for alias in node.names:
                if source.endswith("pipeline"):
                    names.add(alias.asname or alias.name)
                elif alias.name == "pipeline":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith(".pipeline"):
                    modules.add(alias.asname or alias.name)
    return names, modules


def _resolves_to_pipeline(call: ast.Call, path: Path, names: set, modules: set) -> bool:
    if isinstance(call.func, ast.Name):
        return path == _PIPELINE or call.func.id in names
    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
        return call.func.value.id in modules
    return False


def _identity_calls():
    """Yield ``(path, enclosing function, call)`` for every call in src/ that
    reaches one of the pipeline's identity functions (a same-named helper in
    another module, such as the GUI's path ``_dedup_key``, is not one)."""
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names, modules = _pipeline_bindings(tree)
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and _call_name(node) in _IDENTITY_FUNCTIONS
                    and _resolves_to_pipeline(node, path, names, modules)
                ):
                    yield path, function.name, node


class TestEveryCallSitePassesTheContext:
    def test_identity_helpers_always_receive_a_context(self):
        missing = []
        for path, function, call in _identity_calls():
            name = _call_name(call)
            keywords = {kw.arg for kw in call.keywords}
            if name in _KEYWORD_CONTEXT_CALLS:
                ok = "context" in keywords
            else:
                ok = "context" in keywords or len(call.args) >= _POSITIONAL_CONTEXT_CALLS[name]
            if not ok:
                missing.append(f"{path.relative_to(_SRC)}:{call.lineno} {function} -> {name}")
        assert missing == []

    def test_stage_code_derives_the_context_from_the_submission(self):
        """Outside the identity helpers themselves (which pass their own
        ``context`` through), the only permitted source is the submission."""
        stage_calls = []
        for path, function, call in _identity_calls():
            if function in _IDENTITY_FUNCTIONS:
                continue
            (value,) = [kw.value for kw in call.keywords if kw.arg == "context"]
            assert isinstance(value, ast.Call), (path, call.lineno)
            assert _call_name(value) == "finding_identity_context_for_submission", (
                path,
                call.lineno,
            )
            stage_calls.append((function, _call_name(call)))
        # The tripwire must not pass vacuously: the three stages exist.
        assert sorted(stage_calls) == [
            ("collect_review_batch_results", "_deduplicate_findings"),
            ("run_compliance_for_batch", "assign_compliance_finding_ids"),
            ("run_cross_check_for_batch", "assign_cross_check_finding_ids"),
        ]


# ---------------------------------------------------------------------------
# Ids move only where the old key was wrong (release-note claim)
# ---------------------------------------------------------------------------

_LEGACY_STRIP = re.compile(r"\d{2}\s?\d{2}\s?\d{2}[^.]*\.docx", re.IGNORECASE)


def _legacy_id(f: Finding) -> str:
    """The id the pre-S02 rule minted, reproduced for comparison only."""
    issue = re.sub(r"\s+", " ", _LEGACY_STRIP.sub("", f.issue)).strip().lower()
    key = (issue,) + pipeline._dedup_key(f)[1:]
    return "rf-" + hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:12]


class TestIdStability:
    @pytest.mark.parametrize(
        "issue",
        [
            "Duplicate paragraph in 210500.docx.",
            "210500.docx cites the 2019 edition of NFPA 13.",
            "Stale edition reference.",
        ],
    )
    def test_an_id_the_old_rule_got_right_is_unchanged(self, issue):
        finding = _finding(issue)
        assert compute_finding_id(finding, context=_context("210500.docx")) == (
            _legacy_id(finding)
        )

    def test_an_id_the_old_rule_got_wrong_changes(self):
        copper, pvc = _finding(COPPER), _finding(PVC)
        assert _legacy_id(copper) == _legacy_id(pvc)  # the defect
        context = _context("210500.docx")
        assert compute_finding_id(copper, context=context) != (
            compute_finding_id(pvc, context=context)
        )
