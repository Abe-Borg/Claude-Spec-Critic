"""Tests for deterministic checks expansion.

This work:

* adds three new deterministic detectors to ``preprocessor.py`` —
  ``detect_unresolved_template_markers``, ``detect_invalid_code_cycle_strings``,
  and ``detect_duplicate_paragraphs``;
* stamps every alert dict with a stable ``deterministic_rule`` identifier so
  consumers can branch on a rule id instead of sniffing the human label;
* propagates every alert list (LEED, placeholder, code-cycle, structural,
  naming, plus the three new ones) through ``_PreparedSpecs`` →
  ``BatchSubmission`` → ``CollectedBatchState`` → ``PipelineResult``;
* renders every alert category in the report exporter's Alerts section
  with a ``(deterministic check)`` suffix per directive 2;
* extends the verification router's local-skip keyword list so a finding
  whose text mentions one of the new rules does not pay for a Sonnet+
  web_search call.

Coverage is organized into one class per rule + integration smoke checks
for the pipeline plumbing, report rendering, verification routing, and
resume-state round-trip.

Chunk S03 (plan WP-04) added the classes at the end: California long-form
citations, the placeholder policy (bare TBD, whole-word keywords, the
``TBD-200`` identifier rule, the bracketed OPTIONAL decision), CSI file-naming
styles with the neutral mixture notice, and pins that the rule ids and alert
limits did not change. Its heading-structure tests are in
``test_heading_structure.py``, the stale-citation suppression tests in
``test_preprocessor_policy.py``, and ASCE 7 designation syntax in
``test_asce7_stale_editions.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from docx import Document

from src.core.code_cycles import CALIFORNIA_2025
from src.input.preprocessor import (
    DETERMINISTIC_RULE_DUPLICATE_PARAGRAPH,
    DETERMINISTIC_RULE_INCONSISTENT_FILENAME,
    DETERMINISTIC_RULE_INVALID_CODE_CYCLE,
    DETERMINISTIC_RULE_PLACEHOLDER,
    DETERMINISTIC_RULE_STALE_CODE_CYCLE,
    DETERMINISTIC_RULE_TEMPLATE_MARKER,
    PreprocessResult,
    _csi_filename_style,
    detect_duplicate_paragraphs,
    detect_inconsistent_file_naming,
    detect_invalid_code_cycle_strings,
    detect_placeholders,
    detect_stale_code_cycle_references,
    detect_unresolved_template_markers,
    preprocess_spec,
)
from src.review.reviewer import Finding
from src.verification.verification_prescreen import classify_finding_for_verification
from tests.fixtures import spec_docx as fx


# ---------------------------------------------------------------------------
# Rule id wiring
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# detect_unresolved_template_markers
# ---------------------------------------------------------------------------

class TestUnresolvedTemplateMarkers:
    @pytest.mark.parametrize(
        "content, expected_token",
        [
            ("TODO: confirm with PM.", "todo"),
            # Bare ``TODO Confirm`` is also flagged so capitalized
            # continuations don't slip past the colon-only rule.
            ("TODO Confirm hanger spacing.", "todo"),
            ("FIXME before issue.", "fixme"),
            ("Confirm XXX before issue.", "xxx"),
            ("Capacity: ??? gpm.", "???"),
            ("Lorem ipsum dolor sit amet.", "ipsum"),
        ],
    )
    def test_flags_marker(self, content: str, expected_token: str) -> None:
        alerts = detect_unresolved_template_markers(content, "s.docx")
        assert any(expected_token in a["match"].lower() for a in alerts)
        assert all(a["deterministic_rule"] == DETERMINISTIC_RULE_TEMPLATE_MARKER for a in alerts)

    def test_does_not_flag_lowercase_to_do_phrase(self) -> None:
        # "things to do" in prose must not trigger.
        alerts = detect_unresolved_template_markers(
            "There are several things to do before submittal.", "s.docx"
        )
        assert alerts == []

    def test_does_not_flag_model_number_like_xxx_dash(self) -> None:
        # Model numbers ("XXX-12") and digits ("XXX2") should not trigger.
        alerts = detect_unresolved_template_markers("Model XXX-12 specified.", "s.docx")
        assert alerts == []

    def test_does_not_flag_double_question(self) -> None:
        # Two question marks ("Is it correct??") are not the placeholder
        # we want to flag.
        alerts = detect_unresolved_template_markers("Is this correct??", "s.docx")
        assert alerts == []

    def test_alert_dict_has_expected_keys(self) -> None:
        alerts = detect_unresolved_template_markers("TODO: fix.", "spec.docx")
        assert alerts
        a = alerts[0]
        assert a["filename"] == "spec.docx"
        assert a["deterministic_rule"] == DETERMINISTIC_RULE_TEMPLATE_MARKER
        assert a["position"] == 0
        assert "context" in a


# ---------------------------------------------------------------------------
# detect_invalid_code_cycle_strings
# ---------------------------------------------------------------------------

class TestInvalidCodeCycleStrings:
    @pytest.mark.parametrize(
        "content, year",
        [
            # California never published a 2018 cycle (abbreviated form).
            ("Per the 2018 CBC.", "2018"),
            ("See 2020 CMC for venting.", "2020"),
            # The full-name pattern ("2024 California Building Code") is a
            # distinct regex branch and must also surface as invalid.
            ("Comply with 2024 California Building Code.", "2024"),
        ],
    )
    def test_flags_invalid_year(self, content: str, year: str) -> None:
        alerts = detect_invalid_code_cycle_strings(content, "s.docx")
        assert any(year in a["match"] for a in alerts)
        assert all(a["deterministic_rule"] == DETERMINISTIC_RULE_INVALID_CODE_CYCLE for a in alerts)
        assert all(a["found_year"] == year for a in alerts)

    def test_does_not_flag_real_cycle_years(self) -> None:
        # Each of these is a real California cycle year and must not trigger.
        for year in ("2010", "2013", "2016", "2019", "2022", "2025", "2028"):
            alerts = detect_invalid_code_cycle_strings(f"Per {year} CBC.", "s.docx")
            assert alerts == [], f"unexpectedly flagged real cycle year {year}"

    def test_does_not_flag_year_without_code_abbrev(self) -> None:
        # A bare year ("In 2018, the school...") with no code reference
        # must not trigger.
        alerts = detect_invalid_code_cycle_strings(
            "In 2018, the school was renovated.", "s.docx"
        )
        assert alerts == []

    def test_disjoint_from_stale_cycle_detector(self) -> None:
        # 2019 CBC is *stale* (real history). 2018 CBC is *invalid*. The two
        # detectors must not double-count the same span. Inputs are
        # constructed so each detector only sees the year it owns.
        content = "Per the 2019 CBC. Per the 2018 CBC."
        stale = detect_stale_code_cycle_references(content, "s.docx", CALIFORNIA_2025)
        invalid = detect_invalid_code_cycle_strings(content, "s.docx")
        stale_years = {a.get("found_year") for a in stale}
        invalid_years = {a.get("found_year") for a in invalid}
        assert "2019" in stale_years and "2018" not in stale_years
        assert "2018" in invalid_years and "2019" not in invalid_years


# ---------------------------------------------------------------------------
# detect_duplicate_paragraphs
# ---------------------------------------------------------------------------

class TestDuplicateParagraphs:
    def test_flags_repeated_long_paragraph(self) -> None:
        para = (
            "Submittals shall be provided for all piping accessories within "
            "10 days of award and shall include manufacturer cut sheets."
        )
        content = f"1.01 GENERAL\n\n{para}\n\n2.01 PRODUCTS\n\n{para}"
        alerts = detect_duplicate_paragraphs(content, "s.docx")
        assert len(alerts) == 1
        assert alerts[0]["deterministic_rule"] == DETERMINISTIC_RULE_DUPLICATE_PARAGRAPH
        assert alerts[0]["occurrence_count"] == 2

    def test_does_not_flag_short_repeats(self) -> None:
        # Short headings ("PART 1") repeat by design and must not trigger.
        content = "PART 1\n\nbody\n\nPART 1\n\nmore body"
        assert detect_duplicate_paragraphs(content, "s.docx") == []

    def test_normalizes_whitespace_and_case(self) -> None:
        para = (
            "Provide all anchorage hardware in stainless steel where exposed "
            "to weather, per the structural drawings and SSF-12."
        )
        content = (
            f"{para}\n\n"
            # Same paragraph but with extra whitespace and different case.
            f"  PROVIDE   ALL  anchorage  hardware  in  stainless  steel  "
            f"WHERE  exposed  to  weather,  per  the  structural  drawings  "
            f"and  SSF-12.   "
        )
        alerts = detect_duplicate_paragraphs(content, "s.docx")
        assert len(alerts) == 1

    def test_respects_min_length_kwarg(self) -> None:
        para = "Short clause but long enough."
        content = f"{para}\n\n{para}"
        # default min_length=80 → no alert
        assert detect_duplicate_paragraphs(content, "s.docx") == []
        # custom 20 → flagged
        alerts = detect_duplicate_paragraphs(content, "s.docx", min_length=20)
        assert len(alerts) == 1


# ---------------------------------------------------------------------------
# preprocess_spec aggregator
# ---------------------------------------------------------------------------

class TestPreprocessSpecAggregator:
    def test_aggregates_all_chunk_o_alerts(self) -> None:
        long_dup = (
            "Provide a complete and operational system tested per the "
            "manufacturer's instructions before substantial completion."
        )
        content = (
            "1.01 GENERAL\n\n"
            f"{long_dup}\n\n"
            "TODO: confirm pipe sizing.\n\n"
            "Per the 2018 CBC.\n\n"
            "[INSERT PROJECT NAME]\n\n"
            f"{long_dup}\n\n"
            "LEED Silver targeted."
        )
        result = preprocess_spec(content, "23 21 13.docx", cycle=CALIFORNIA_2025)
        assert isinstance(result, PreprocessResult)
        assert result.template_marker_alerts, "template marker not detected"
        assert result.invalid_code_cycle_alerts, "invalid code cycle not detected"
        assert result.duplicate_paragraph_alerts, "duplicate paragraph not detected"
        # Existing detectors keep working alongside the new ones.
        assert result.placeholder_alerts
        assert result.leed_alerts

    def test_no_cycle_still_runs_chunk_o_detectors(self) -> None:
        # The new detectors do not require a cycle, so callers that pass
        # ``cycle=None`` still get template / duplicate / (no-op invalid)
        # results without crashing.
        content = "TODO: confirm.\n\n" + "x" * 200
        result = preprocess_spec(content, "s.docx")
        assert result.template_marker_alerts
        # invalid_code_cycle_alerts is fine to be empty when no code cite.
        assert result.invalid_code_cycle_alerts == []


# ---------------------------------------------------------------------------
# Pipeline plumbing — alerts flow from prepare → submission → result
# ---------------------------------------------------------------------------

class TestPipelinePlumbing:
    def test_finalize_batch_result_forwards_chunk_o_alerts(self) -> None:
        """finalize_batch_result copies every alert list onto the result."""
        from src.batch.batch import BatchJob
        from src.orchestration.pipeline import BatchSubmission, CollectedBatchState, finalize_batch_result
        from src.review.reviewer import ReviewResult

        sub = BatchSubmission(
            job=BatchJob(batch_id="msgbatch_test", job_type="review", request_map={}, created_at=0.0),
        )
        sentinel_codecycle = [{"filename": "s.docx", "match": "2019 CBC", "deterministic_rule": DETERMINISTIC_RULE_STALE_CODE_CYCLE}]
        sentinel_template = [{"filename": "s.docx", "match": "TODO: x", "deterministic_rule": DETERMINISTIC_RULE_TEMPLATE_MARKER}]
        state = CollectedBatchState(
            submission=sub,
            review_result=ReviewResult(findings=[]),
            code_cycle_alerts=sentinel_codecycle,
            template_marker_alerts=sentinel_template,
        )
        result = finalize_batch_result(state)
        assert result.code_cycle_alerts == sentinel_codecycle
        assert result.template_marker_alerts == sentinel_template

    def test_collect_review_batch_results_forwards_submission_alerts(self, monkeypatch) -> None:
        """collect_review_batch_results copies submission alerts onto state."""
        from src.batch.batch import BatchJob
        from src.orchestration.pipeline import BatchSubmission, collect_review_batch_results

        # Stub the network-facing retrieve_review_results so this test stays
        # hermetic. An empty result map means no findings are produced.
        monkeypatch.setattr("src.orchestration.pipeline.retrieve_review_results", lambda job, model: {})
        monkeypatch.setattr(
            "src.orchestration.pipeline._recover_retryable_review_batch_results",
            lambda submission, results, log: results,
        )

        sub = BatchSubmission(
            job=BatchJob(batch_id="msgbatch_test", job_type="review", request_map={}, created_at=0.0),
            code_cycle_alerts=[{"filename": "s.docx", "match": "2019 CBC"}],
            duplicate_paragraph_alerts=[{"filename": "s.docx", "match": "x" * 80}],
        )
        state = collect_review_batch_results(sub)
        assert state.code_cycle_alerts == sub.code_cycle_alerts
        assert state.duplicate_paragraph_alerts == sub.duplicate_paragraph_alerts


# ---------------------------------------------------------------------------
# Verification router — new keywords route to local_skip
# ---------------------------------------------------------------------------

class TestVerificationRouterChunkO:
    """The router treats GRIPES findings about the new rules as local_skip."""

    @pytest.fixture
    def gripe_finding(self) -> Finding:
        return Finding(
            severity="GRIPES",
            fileName="s.docx",
            section="2.1",
            issue="placeholder",
            actionType="EDIT",
            existingText=None,
            replacementText=None,
            codeReference=None,
        )

    @pytest.mark.parametrize(
        "issue",
        [
            "Unresolved TODO marker in section 2.1",
            "FIXME left in the spec",
            "Invalid code cycle year 2018",
            "Duplicate paragraph in submittals section",
        ],
    )
    def test_chunk_o_keyword_routes_to_local_skip(
        self, gripe_finding: Finding, issue: str
    ) -> None:
        gripe_finding.issue = issue
        assert classify_finding_for_verification(gripe_finding) == "local_skip"

    def test_high_severity_overrides_local_skip(self, gripe_finding: Finding) -> None:
        # Severity gate: anything above GRIPES needs web verification even if
        # the issue text looks local.
        gripe_finding.severity = "HIGH"
        gripe_finding.issue = "Duplicate paragraph in submittals section"
        assert classify_finding_for_verification(gripe_finding) == "web_required"

    def test_code_reference_overrides_local_skip(self, gripe_finding: Finding) -> None:
        gripe_finding.issue = "FIXME — cycle reference may be wrong"
        gripe_finding.codeReference = "CBC 1605"
        assert classify_finding_for_verification(gripe_finding) == "web_required"


# ---------------------------------------------------------------------------
# Report exporter integration — every alert section renders
# ---------------------------------------------------------------------------

class _StubPipelineResult:
    """Duck-typed PipelineResult that exercises every alert section.

    Kept inline here so the chunk O snapshot tests don't share a fixture
    with the chunk N tests (each chunk owns its own surface).
    """

    def __init__(self, **kwargs) -> None:
        from src.review.reviewer import ReviewResult

        self.review_result = kwargs.get("review_result", ReviewResult(findings=[]))
        self.cross_check_result = None
        self.files_reviewed = kwargs.get("files_reviewed", ["s.docx"])
        self.cycle_label = "2025"
        self.total_elapsed_seconds = 1.0
        self.leed_alerts = kwargs.get("leed_alerts", [])
        self.placeholder_alerts = kwargs.get("placeholder_alerts", [])
        self.code_cycle_alerts = kwargs.get("code_cycle_alerts", [])
        self.structural_alerts = kwargs.get("structural_alerts", [])
        self.naming_alerts = kwargs.get("naming_alerts", [])
        self.template_marker_alerts = kwargs.get("template_marker_alerts", [])
        self.invalid_code_cycle_alerts = kwargs.get("invalid_code_cycle_alerts", [])
        self.duplicate_paragraph_alerts = kwargs.get("duplicate_paragraph_alerts", [])


def _alert(text: str, rule_id: str, filename: str = "s.docx") -> dict:
    return {
        "filename": filename,
        "type": "test alert",
        "match": text,
        "context": text,
        "position": 0,
        "deterministic_rule": rule_id,
    }


def _doc_text(path: Path) -> str:
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


class TestReportExporterChunkOIntegration:
    def test_export_renders_template_marker_section(self, tmp_path: Path) -> None:
        from src.output.report_exporter import export_report

        result = _StubPipelineResult(
            template_marker_alerts=[_alert("TODO: confirm hanger spacing.", DETERMINISTIC_RULE_TEMPLATE_MARKER)],
        )
        out = tmp_path / "report.docx"
        export_report(result, out)
        text = _doc_text(out)
        assert "Unresolved Template Markers" in text
        assert "(deterministic check)" in text
        assert "TODO: confirm hanger spacing." in text

    def test_export_skips_alerts_heading_when_no_alerts(self, tmp_path: Path) -> None:
        from src.output.report_exporter import export_report

        result = _StubPipelineResult()  # every list empty
        out = tmp_path / "report.docx"
        export_report(result, out)
        text = _doc_text(out)
        # No alerts → no top-level "Alerts" heading, no deterministic-check
        # banner. (Other report sections may still render.)
        assert "(deterministic check)" not in text


# ---------------------------------------------------------------------------
# California long-form citations (plan WP-04C, chunk S03)
# ---------------------------------------------------------------------------


class TestCaliforniaLongFormCitations:
    """California writes its code cycle out in long forms the year/code
    patterns missed. They live in the California module's vocabulary only."""

    @pytest.mark.parametrize(
        "citation, year",
        [
            ("2019 California Building Standards Code", "2019"),
            ("2022 California Green Building Standards Code", "2022"),
            ("2022 Edition of the CBC", "2022"),
            ("2019 edition of CMC", "2019"),
            ("CBC (2022 edition)", "2022"),
            ("CPC (2019)", "2019"),
            ("Title 24, 2022", "2022"),
            ("Title-24 2019", "2019"),
            ("2022 Title 24", "2022"),
            ("2019 California Title 24", "2019"),
        ],
    )
    def test_a_stale_long_form_is_flagged(self, citation, year):
        (alert,) = detect_stale_code_cycle_references(
            f"Comply with the {citation} requirements.", "s.docx", CALIFORNIA_2025
        )
        assert alert["match"] == citation
        assert alert["found_year"] == year
        assert alert["deterministic_rule"] == DETERMINISTIC_RULE_STALE_CODE_CYCLE

    @pytest.mark.parametrize(
        "citation",
        ["Title 24, 2025", "2025 Edition of the CBC", "CBC (2025 edition)", "2025 Title 24"],
    )
    def test_the_current_cycle_is_not_flagged(self, citation):
        text = f"Comply with the {citation} requirements."
        assert detect_stale_code_cycle_references(text, "s.docx", CALIFORNIA_2025) == []
        assert detect_invalid_code_cycle_strings(text, "s.docx") == []

    @pytest.mark.parametrize(
        "citation, year",
        [("Title 24, 2024", "2024"), ("CBC (2023 edition)", "2023"), ("2021 Edition of the CBC", "2021")],
    )
    def test_a_year_that_is_no_cycle_is_invalid(self, citation, year):
        (alert,) = detect_invalid_code_cycle_strings(f"Comply with {citation}.", "s.docx")
        assert alert["found_year"] == year
        assert alert["type"] == f"Invalid California code cycle year ({year})"

    @pytest.mark.parametrize(
        "text",
        [
            "Comply with Title 24 requirements.",
            "Comply with Title 24 Part 6.",
            "Comply with Title 245, 2022.",
            "Comply with the 2022 edition of the drawings.",
            "Submit the CBC checklist (2022 submittal).",
        ],
    )
    def test_no_year_or_no_code_is_not_a_citation(self, text):
        assert detect_stale_code_cycle_references(text, "s.docx", CALIFORNIA_2025) == []
        assert detect_invalid_code_cycle_strings(text, "s.docx") == []

    def test_title_24_is_not_equated_with_the_cbc(self):
        (alert,) = detect_stale_code_cycle_references(
            "Comply with Title 24, 2022.", "s.docx", CALIFORNIA_2025
        )
        assert alert["match"] == "Title 24, 2022"
        assert "CBC" not in alert["type"]

    def test_no_other_module_reads_california_forms(self):
        from src.modules.registry import AVAILABLE_MODULES

        text = (
            "Comply with Title 24, 2023, the 2021 Edition of the CBC, CBC (2023 edition), "
            "and the 2019 California Building Standards Code."
        )
        others = [m for m in AVAILABLE_MODULES.values() if m.module_id != "california_k12_mep"]
        assert others, "precondition: the registry has other modules"
        for module in others:
            result = preprocess_spec(text, "s.docx", cycle=module.cycle)
            assert result.code_cycle_alerts == [], module.module_id
            assert result.invalid_code_cycle_alerts == [], module.module_id


class TestListPunctuationIsNotACitation:
    """In "2019 CBC, 2019 CMC" the "<code> <year>" pattern also matches
    "CBC, 2019": the first citation's code with the next one's year. It
    overlaps both real citations without being contained in either, and was
    reported as a third citation (found in chunk S03's review). Both year/code
    detectors now skip a match that overlaps a recorded one."""

    def test_a_comma_list_of_stale_citations_is_reported_once_each(self):
        alerts = detect_stale_code_cycle_references(
            "Comply with the 2019 CBC, 2019 CMC, and 2019 CPC.", "s.docx", CALIFORNIA_2025
        )
        assert [a["match"] for a in alerts] == ["2019 CBC", "2019 CMC", "2019 CPC"]

    def test_a_comma_list_of_invalid_years_is_reported_once_each(self):
        alerts = detect_invalid_code_cycle_strings("Comply with 2018 CBC, 2018 CMC.", "s.docx")
        assert [a["match"] for a in alerts] == ["2018 CBC", "2018 CMC"]

    def test_the_same_holds_for_the_other_modules(self):
        from src.modules import get_module

        vocabulary = get_module("datacenter_fire").detector_vocabulary
        alerts = detect_invalid_code_cycle_strings(
            "Comply with 2021 IBC, 2019 IFC.", "s.docx", vocabulary=vocabulary
        )
        assert [a["match"] for a in alerts] == ["2019 IFC"]

    def test_one_citation_written_two_ways_is_one_alert(self):
        alerts = detect_stale_code_cycle_references(
            "Comply with the 2022 CBC (2022 edition).", "s.docx", CALIFORNIA_2025
        )
        assert [a["match"] for a in alerts] == ["2022 CBC"]

    def test_a_code_before_its_year_is_still_a_citation(self):
        alerts = detect_stale_code_cycle_references(
            "Comply with CBC 2019 and CMC, 2019.", "s.docx", CALIFORNIA_2025
        )
        assert [a["match"] for a in alerts] == ["CBC 2019", "CMC, 2019"]


# ---------------------------------------------------------------------------
# Placeholder policy (plan WP-04D, chunk S03)
# ---------------------------------------------------------------------------


def _placeholders(content: str) -> list[tuple[str, str]]:
    return [(a["type"], a["match"]) for a in detect_placeholders(content, "p.docx")]


class TestPlaceholderPolicy:
    @pytest.mark.parametrize(
        "content",
        [
            "Pipe size: TBD by engineer.",
            "Pipe size: tbd.",
            "Size TBD - see drawings.",
            "Finish TBD\u2014by Architect.",
            "Motor voltage (TBD).",
            "[Pipe size TBD]",
        ],
    )
    def test_a_bare_tbd_is_detected_once(self, content):
        (alert,) = detect_placeholders(content, "p.docx")
        assert alert["type"] == "TBD placeholder"
        assert alert["match"].upper() == "TBD"
        assert alert["deterministic_rule"] == DETERMINISTIC_RULE_PLACEHOLDER

    @pytest.mark.parametrize(
        "content, expected",
        [
            ("Pipe size: [TBD].", [("TBD placeholder", "[TBD]")]),
            ("Pipe size: [TBD-1].", [("TBD placeholder", "[TBD-1]")]),
            ("Pipe size: [TO BE DETERMINED].", [("TBD placeholder", "[TO BE DETERMINED]")]),
            ("Pipe size: [INSERT SIZE TBD].", [("INSERT placeholder", "[INSERT SIZE TBD]")]),
            ("Pipe size: <VERIFY TBD>.", [("VERIFY tag", "<VERIFY TBD>")]),
        ],
    )
    def test_a_tbd_inside_a_marker_is_not_counted_twice(self, content, expected):
        assert _placeholders(content) == expected

    @pytest.mark.parametrize(
        "content",
        [
            "Provide model TBDF-200 fitting.",
            # Policy: a TBD joined to a hyphenated token is an identifier,
            # like the XXX-12 model number the template-marker rule skips.
            "Provide model TBD-200 fitting.",
            "Provide model 200-TBD fitting.",
            "Provide model TBD200 fitting.",
        ],
    )
    def test_a_tbd_identifier_stays_clean(self, content):
        assert detect_placeholders(content, "p.docx") == []

    @pytest.mark.parametrize(
        "content",
        [
            "See [EDITION 2024].",
            "See [SELECTED ITEMS].",
            "See [INSERTION LOSS DATA].",
            "See [VERIFYING AGENCY].",
            "See [COORDINATES].",
            "See [OPTIONALLY FURNISHED].",
            "See <EDITION>.",
            "See <INSERTS>.",
        ],
    )
    def test_a_keyword_must_be_a_whole_word(self, content):
        assert detect_placeholders(content, "p.docx") == []

    @pytest.mark.parametrize(
        "content, expected_type",
        [
            ("Provide [INSERT PROJECT NAME].", "INSERT placeholder"),
            ("Provide [VERIFY].", "VERIFY placeholder"),
            ("Provide [EDIT: retain one].", "EDIT placeholder"),
            ("Provide [SELECT].", "SELECT placeholder"),
            ("Provide [SELECT ONE] finish.", "SELECT placeholder"),
            ("Provide [COORDINATE WITH CIVIL].", "COORDINATE placeholder"),
            ("Provide [N/A].", "N/A placeholder"),
            ("Provide [OPTION A].", "OPTION placeholder"),
            ("Provide [OPTIONS: A OR B].", "OPTION placeholder"),
            # Decision: a bracketed OPTIONAL marks a keep-or-delete choice
            # the specifier still has to make, so it stays a placeholder.
            ("Provide [OPTIONAL].", "OPTION placeholder"),
            ("Provide [OPTIONAL: RETAIN FOR HOSPITAL WORK].", "OPTION placeholder"),
            ("Provide <VERIFY>.", "VERIFY tag"),
            ("Provide <EDIT>.", "EDIT tag"),
            ("Provide <INSERT NAME>.", "INSERT tag"),
            ("Provide ____ units.", "Underscore placeholder"),
            ("Provide [...].", "Ellipsis placeholder"),
        ],
    )
    def test_existing_markers_still_flag(self, content, expected_type):
        (alert,) = detect_placeholders(content, "p.docx")
        assert alert["type"] == expected_type

    def test_bare_tbd_alerts_come_after_the_existing_patterns(self):
        # The bare-TBD pattern is last, so every alert the older patterns
        # produce keeps its place in the list.
        assert _placeholders("Size TBD. Provide [SELECT ONE]. Finish ____.") == [
            ("SELECT placeholder", "[SELECT ONE]"),
            ("Underscore placeholder", "____"),
            ("TBD placeholder", "TBD"),
        ]

    def test_the_alert_limit_is_unchanged(self):
        content = " ".join(["TBD"] * 250)
        assert len(detect_placeholders(content, "p.docx")) == 200


# ---------------------------------------------------------------------------
# File naming (plan WP-04E, chunk S03)
# ---------------------------------------------------------------------------


class TestFileNamingStyles:
    #: The fixtures' naming styles, as the detector names them.
    _FIXTURE_STYLE = {
        "separated": {"space"},
        "dashed": {"dash"},
        "compact": {"compact"},
        "section_prefixed": {"section-space", "section-compact"},
        "unrecognized": {None},
    }

    @pytest.mark.parametrize("example", fx.FILENAME_EXAMPLES, ids=lambda e: e.name)
    def test_every_fixture_name_gets_its_declared_style(self, example):
        assert _csi_filename_style(example.name) in self._FIXTURE_STYLE[example.style]

    @pytest.mark.parametrize(
        "name, style",
        [
            ("21 05 00.DOCX", "space"),
            ("21\t05\t00.docx", "space"),
            ("23 - 21 - 13 Piping.docx", "dash"),
            ("210500_Common_Work.docx", "compact"),
            ("SECTION 211316.DOCX", "section-compact"),
            ("Section-21-13-16.docx", "section-dash"),
            ("2105001.docx", None),
            ("21 05 00a.docx", None),
            ("15400 - Plumbing.docx", None),
        ],
    )
    def test_styles(self, name, style):
        assert _csi_filename_style(name) == style

    def test_one_style_raises_no_notice_whatever_the_extension_case(self):
        names = ["21 05 00.docx", "21 13 13.DOCX", "21 13 16.Docx"]
        assert detect_inconsistent_file_naming(names) == []

    def test_a_majority_style_flags_the_others(self):
        names = ["21 05 00.docx", "21 13 13.docx", "211316.docx"]
        (alert,) = detect_inconsistent_file_naming(names)
        assert alert == {
            "filename": "211316.docx",
            "type": "Inconsistent CSI filename style (expected space-separated)",
            "match": "211316.docx",
            "context": "211316.docx — compact; most files are space-separated",
            "position": 0,
            "dominant_style": "space",
            "found_style": "compact",
            "deterministic_rule": DETERMINISTIC_RULE_INCONSISTENT_FILENAME,
        }

    def test_the_fixture_mixture_is_a_neutral_notice(self):
        names = ["21 05 00.docx", "211313.docx", "SECTION 21 13 16.DOCX"]
        alerts = detect_inconsistent_file_naming(names)
        assert [a["filename"] for a in alerts] == names
        summary = "1 space-separated, 1 compact, 1 space-separated with a SECTION prefix"
        for alert in alerts:
            assert alert["type"] == "Mixed CSI filename styles (no dominant style)"
            assert alert["dominant_style"] is None
            assert alert["context"].endswith(f"no single style dominates ({summary})")
            assert alert["deterministic_rule"] == DETERMINISTIC_RULE_INCONSISTENT_FILENAME
        assert [a["found_style"] for a in alerts] == ["space", "compact", "section-space"]

    @pytest.mark.parametrize(
        "names",
        [
            ["21 05 00.docx", "21-13-13.docx"],
            ["21 05 00.docx", "21 13 13.docx", "211316.docx", "21-30-00.docx"],
        ],
    )
    def test_no_majority_invents_no_convention(self, names):
        # A tie, and a plurality that is not a majority (2 of 4).
        alerts = detect_inconsistent_file_naming(names)
        assert [a["filename"] for a in alerts] == names
        assert all(a["dominant_style"] is None for a in alerts)

    def test_unknown_names_neither_vote_nor_hide_a_mixture(self):
        names = [
            "21 05 00.docx",
            "21-13-13.docx",
            "Fire Protection Narrative.docx",
            "NFPA 13 Checklist.docx",
            "2024-05-01 Addendum 2.docx",
        ]
        alerts = detect_inconsistent_file_naming(names)
        assert [a["filename"] for a in alerts] == ["21 05 00.docx", "21-13-13.docx"]

    def test_unknown_names_are_not_flagged_beside_a_majority(self):
        names = ["21 05 00.docx", "21 13 13.docx", "21-13-16.docx", "Narrative.docx"]
        assert [a["filename"] for a in detect_inconsistent_file_naming(names)] == ["21-13-16.docx"]

    def test_a_repeated_name_is_reported_once_in_input_order(self):
        names = ["211316.docx", "21 05 00.docx", "211316.docx", "21 13 13.docx", "21 13 14.docx"]
        assert [a["filename"] for a in detect_inconsistent_file_naming(names)] == ["211316.docx"]

    def test_fewer_than_two_csi_names_raise_nothing(self):
        assert detect_inconsistent_file_naming(["21 05 00.docx"]) == []
        assert detect_inconsistent_file_naming(["21 05 00.docx", "Narrative.docx"]) == []
        assert detect_inconsistent_file_naming([]) == []


class TestNamingNoticeInTheReport:
    def test_both_exporters_share_a_description_that_fits_a_mixture(self):
        from src.output import html_report_exporter
        from src.output.report_exporter import NAMING_ALERTS_DESCRIPTION
        from src.modules import get_module

        sections = dict(
            (key, description)
            for key, _title, description in html_report_exporter._alert_sections_spec(
                get_module(None)
            )
        )
        assert sections["naming"] == NAMING_ALERTS_DESCRIPTION
        assert "dominant" not in NAMING_ALERTS_DESCRIPTION

    def test_the_docx_report_renders_each_entry_with_its_context(self, tmp_path: Path) -> None:
        from src.output.report_exporter import NAMING_ALERTS_DESCRIPTION, export_report

        names = ["21 05 00.docx", "211313.docx"]
        result = _StubPipelineResult(
            files_reviewed=names, naming_alerts=detect_inconsistent_file_naming(names)
        )
        out = tmp_path / "report.docx"
        export_report(result, out)
        text = _doc_text(out)
        assert NAMING_ALERTS_DESCRIPTION in text
        assert "211313.docx — compact; no single style dominates" in text


# ---------------------------------------------------------------------------
# The alert contract is unchanged (plan WP-04, chunk S03)
# ---------------------------------------------------------------------------


class TestAlertContractUnchanged:
    def test_rule_ids(self):
        from src.input import preprocessor

        assert {
            name: getattr(preprocessor, name)
            for name in dir(preprocessor)
            if name.startswith("DETERMINISTIC_RULE_")
        } == {
            "DETERMINISTIC_RULE_LEED": "leed_reference",
            "DETERMINISTIC_RULE_PLACEHOLDER": "placeholder",
            "DETERMINISTIC_RULE_STALE_CODE_CYCLE": "stale_code_cycle",
            "DETERMINISTIC_RULE_STALE_ASCE7": "stale_asce7",
            "DETERMINISTIC_RULE_EMPTY_SECTION": "empty_section",
            "DETERMINISTIC_RULE_DUPLICATE_HEADING": "duplicate_heading",
            "DETERMINISTIC_RULE_TEMPLATE_MARKER": "template_marker",
            "DETERMINISTIC_RULE_INVALID_CODE_CYCLE": "invalid_code_cycle",
            "DETERMINISTIC_RULE_DUPLICATE_PARAGRAPH": "duplicate_paragraph",
            "DETERMINISTIC_RULE_INCONSISTENT_FILENAME": "inconsistent_filename",
            "DETERMINISTIC_RULE_WRONG_POLITY": "wrong_polity_token",
        }

    def test_alert_limits(self):
        import inspect

        from src.input import preprocessor

        limits = {
            name: inspect.signature(getattr(preprocessor, name)).parameters["max_matches"].default
            for name in (
                "detect_leed_references",
                "detect_placeholders",
                "detect_stale_code_cycle_references",
                "detect_empty_sections",
                "detect_duplicate_headings",
                "detect_unresolved_template_markers",
                "detect_invalid_code_cycle_strings",
                "detect_duplicate_paragraphs",
                "detect_wrong_polity_tokens",
            )
        }
        assert limits == {
            "detect_leed_references": 50,
            "detect_placeholders": 200,
            "detect_stale_code_cycle_references": 200,
            "detect_empty_sections": 50,
            "detect_duplicate_headings": 50,
            "detect_unresolved_template_markers": 200,
            "detect_invalid_code_cycle_strings": 100,
            "detect_duplicate_paragraphs": 50,
            "detect_wrong_polity_tokens": 100,
        }
