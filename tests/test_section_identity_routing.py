"""Routing from a specification's own SECTION heading (plan WP-05, chunk S13).

A routed program sends each spec to the review module(s) for its discipline.
Before S13 the router read only the file name, so ``210500.docx`` whose body
opens with "SECTION 21 05 00" was unsupported, a separated file name that
disagreed with the document was followed silently, and two different files
named ``spec.docx`` were bound to one path depending on input order.

What this module pins, each through the code a real run uses:

* **One rule** (``src/input/section_identity.py``) for what a section number
  is, on every surface, and for the heading: read from a bounded opening,
  heading-shaped text only, carried on ``ExtractedSpec.section_heading``
  with the element ids it came from.
* **Routing at the assignment seam** — real DOCX, real extraction,
  ``assignments_for_specs`` — for compact names, corroboration, the guards
  against dates / project numbers / NFPA references, related-section
  references, contradictions, and the Division 27 / 28 rules.
* **Source identity** — distinct files sharing a name refused before
  anything is routed or paid for, at every non-GUI entry point, in either
  input order.
* **Provenance** — the heading evidence (source, signal, element ids)
  survives a saved program manifest and its resume.

Hermetic: no API key, no network, documents written only under ``tmp_path``.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.input import section_identity as si
from src.input.extractor import extract_text_from_docx
from src.input.input_files import (
    BasenameCollisionError,
    basename_key,
    unique_spec_inputs,
)
from src.programs import (
    HYPERSCALE_DATACENTER_PROGRAM,
    RoutingEvidenceSource,
    RoutingState,
    SpecAssignment,
    SpecRoutingInput,
    assignments_for_specs,
    route_spec,
)
from tests.fixtures import spec_docx as fx

FIRE = "datacenter_fire"
ARCH = "datacenter_architecture"
ELEC = "datacenter_electrical"
ESS = "datacenter_electronic_safety_security"

CLEAN = fx.clean_three_part_blocks()


def _heading(number: str, title: str) -> tuple:
    return fx.with_section_heading(CLEAN, number, title)


def _write(tmp_path: Path, name: str, blocks_or_builder, *, folder: str = "") -> Path:
    builder = (
        blocks_or_builder
        if isinstance(blocks_or_builder, fx.SpecDocBuilder)
        else fx.build_blocks(blocks_or_builder)
    )
    directory = tmp_path / folder if folder else tmp_path
    return fx.save_docx(builder, directory, name)


def _route(tmp_path: Path, name: str, blocks_or_builder, *, folder: str = ""):
    """Extract a real document and route it the way a program run does."""
    path = _write(tmp_path, name, blocks_or_builder, folder=folder or name)
    spec = extract_text_from_docx(path)
    (assignment,) = assignments_for_specs(
        [spec], [path], program=HYPERSCALE_DATACENTER_PROGRAM
    )
    return spec, assignment.decision


def _sources(decision) -> list[str]:
    return [item.source.value for item in decision.evidence]


# ===========================================================================
# 1. The one rule for section numbers
# ===========================================================================


class TestNumberForms:
    @pytest.mark.parametrize(
        "text,expected,form",
        [
            ("SECTION 21 05 00", "21 05 00", si.FORM_LABELED),
            ("Project Manual - SECTION 21-13-13 - Wet", "21 13 13", si.FORM_LABELED),
            ("SECTION 211313 - WET-PIPE", "21 13 13", si.FORM_LABELED),
            ("07-27-26 - Membrane Air Barriers", "07 27 26", si.FORM_SEPARATED),
            ("07.27.26 Air Barriers", "07 27 26", si.FORM_SEPARATED),
        ],
    )
    def test_a_title_carries_a_labeled_or_leading_separated_number(
        self, text, expected, form
    ):
        number = si.title_number(text)
        assert number is not None
        assert (number.canonical, number.form) == (expected, form)

    @pytest.mark.parametrize(
        "text",
        [
            "072726 Project Reference",  # compact without a label
            "Campus Project 07 27 26 Issue",  # embedded, not leading
            "NFPA 13 Reference Criteria",
            "2024-05-01 Addendum 2",
        ],
    )
    def test_a_title_does_not_carry_a_compact_embedded_or_standard_number(self, text):
        assert si.title_number(text) is None

    @pytest.mark.parametrize(
        "example",
        [example for example in fx.FILENAME_EXAMPLES],
        ids=lambda example: example.name,
    )
    def test_file_names_read_as_the_shared_fixture_says(self, example):
        """The WP-01 filename examples declare what each name means,
        including the guard cases whose digits are a date, a project number,
        or a standard's number."""
        number = si.filename_number(example.name)
        if example.section is None:
            assert number is None
        else:
            assert number is not None and number.canonical == example.section
            expected_form = (
                si.FORM_COMPACT if example.style == "compact" else number.form
            )
            assert number.form == expected_form
            if example.style == "compact":
                assert number.form == si.FORM_COMPACT

    @pytest.mark.parametrize("name", ["210500a.docx", "2105001.docx", "21050.docx"])
    def test_a_compact_number_glued_to_more_characters_is_not_one(self, name):
        assert si.filename_number(name) is None

    @pytest.mark.parametrize(
        "text,expected",
        [("21", ("21",)), ("2113", ("21", "13")), ("211313", ("21", "13", "13"))],
    )
    def test_a_dedicated_field_accepts_compact_values(self, text, expected):
        number = si.dedicated_number(text)
        assert number is not None and number.parts == expected

    def test_a_division_agrees_with_its_sections_but_not_with_another(self):
        division = si.dedicated_number("21")
        assert division.agrees_with(si.title_number("SECTION 21 13 13"))
        assert not division.agrees_with(si.title_number("SECTION 26 05 00"))
        assert not si.title_number("SECTION 21 13 16").agrees_with(
            si.title_number("SECTION 21 13 13")
        )


def _entries(*texts: str, element_type: str = "paragraph"):
    return [
        SimpleNamespace(element_id=f"p{i}", element_type=element_type, text=text)
        for i, text in enumerate(texts)
    ]


class TestReadSectionHeading:
    def test_a_two_paragraph_heading_records_both_element_ids(self):
        heading = si.read_section_heading(
            _entries("SECTION 21 05 00", "COMMON WORK RESULTS FOR FIRE SUPPRESSION")
        )
        assert heading == si.SectionHeading(
            number="21 05 00",
            title="COMMON WORK RESULTS FOR FIRE SUPPRESSION",
            number_text="SECTION 21 05 00",
            number_element_id="p0",
            title_element_id="p1",
        )
        assert heading.location == "p0-p1"

    @pytest.mark.parametrize(
        "line,number,title",
        [
            ("SECTION 211313 - WET-PIPE SPRINKLER SYSTEMS", "21 13 13", "WET-PIPE SPRINKLER SYSTEMS"),
            ("SECTION 21 05 00 \u2013 COMMON WORK RESULTS", "21 05 00", "COMMON WORK RESULTS"),
            ("Section 26 05 00: Common Work Results for Electrical", "26 05 00", "Common Work Results for Electrical"),
            ("SECTION 21 05 00.13 - FIRE SUPPRESSION TESTING", "21 05 00", "FIRE SUPPRESSION TESTING"),
        ],
    )
    def test_a_one_line_heading_carries_its_own_title(self, line, number, title):
        heading = si.read_section_heading(_entries(line, "PART 1 - GENERAL"))
        assert (heading.number, heading.title) == (number, title)
        assert heading.location == "p0"

    def test_a_title_block_above_the_heading_is_allowed(self):
        heading = si.read_section_heading(
            _entries(
                "ACME DATA CENTER CAMPUS - BUILDING 3",
                "Project No. 2024-015",
                "07.20.2026 ISSUED FOR CONSTRUCTION",
                "SECTION 21 13 13",
                "WET-PIPE SPRINKLER SYSTEMS",
                "PART 1 GENERAL",
            )
        )
        assert (heading.number, heading.number_element_id) == ("21 13 13", "p3")

    @pytest.mark.parametrize(
        "texts",
        [
            # A reference to another section, not this document's identity.
            ("See Section 21 13 13 - Wet-Pipe Sprinkler Systems", "PART 1 GENERAL"),
            ("A. Section 21 13 13 - Wet-Pipe Sprinkler Systems.", "PART 1 GENERAL"),
            # A sentence that happens to start with a section number.
            ("Section 21 13 13 applies to all wet-pipe systems.", "PART 1 GENERAL"),
            ("Section 21 13 13 applies to all wet-pipe systems", "PART 1 GENERAL"),
            ("SECTION 21 13 13 SHALL GOVERN WHERE THEY CONFLICT", "PART 1 GENERAL"),
            # Below the opening: the body has begun.
            ("PART 1 GENERAL", "SECTION 21 13 13 - WET-PIPE"),
            ("1.01 RELATED SECTIONS", "SECTION 21 13 13 - WET-PIPE"),
            ("RELATED SECTIONS", "SECTION 21 13 13 - WET-PIPE"),
            ("END OF SECTION", "SECTION 21 13 13 - WET-PIPE"),
        ],
    )
    def test_only_a_heading_shaped_line_in_the_opening_counts(self, texts):
        assert si.read_section_heading(_entries(*texts)) is None

    def test_the_opening_region_is_bounded(self):
        filler = [f"TITLE BLOCK LINE {i}" for i in range(si.OPENING_REGION_ENTRIES)]
        assert si.read_section_heading(_entries(*filler, "SECTION 21 13 13")) is None
        assert (
            si.read_section_heading(_entries(*filler[:-1], "SECTION 21 13 13")).number
            == "21 13 13"
        )

    def test_two_different_headings_in_the_opening_are_not_an_identity(self):
        assert (
            si.read_section_heading(
                _entries("SECTION 21 05 00", "SECTION 21 13 13 - WET-PIPE")
            )
            is None
        )
        repeated = si.read_section_heading(
            _entries("SECTION 21 05 00 - COMMON WORK", "SECTION 21 05 00")
        )
        assert repeated.number == "21 05 00"

    def test_a_table_row_is_never_the_heading(self):
        """A one-cell row reads exactly like a heading paragraph; it is still
        not read, since a table at the top may just as well list sections."""
        entries = [
            SimpleNamespace(
                element_id="t0r0",
                element_type="table_cell",
                text="SECTION 21 13 13 - WET-PIPE SPRINKLER SYSTEMS",
            )
        ]
        assert si.read_section_heading(entries) is None
        entries[0].element_type = "paragraph"
        assert si.read_section_heading(entries).number == "21 13 13"

    def test_supplemental_blocks_are_not_the_opening(self):
        """The body ends at the first delimiter the extractor appends; nothing
        after it (text boxes, notes, headers and footers) is the opening,
        whatever its element type."""
        entries = [
            SimpleNamespace(element_id="meta:tb", element_type="meta", text="====="),
            SimpleNamespace(element_id="tb0p0", element_type="paragraph", text="SECTION 21 13 13"),
        ]
        assert si.read_section_heading(entries) is None

    def test_a_title_must_be_title_shaped(self):
        heading = si.read_section_heading(
            _entries("SECTION 21 05 00", "Provide the complete fire suppression system.")
        )
        assert heading.title == "" and heading.title_element_id == ""


class TestExtractorCarriesTheHeading:
    def test_the_extracted_spec_carries_number_title_and_element_ids(self, tmp_path):
        path = _write(tmp_path, "210500.docx", _heading("21 05 00", "COMMON WORK RESULTS"))
        heading = extract_text_from_docx(path).section_heading
        assert (heading.number, heading.title) == ("21 05 00", "COMMON WORK RESULTS")
        assert (heading.number_element_id, heading.title_element_id) == ("p0", "p1")

    def test_a_heading_inside_a_content_control_is_read(self, tmp_path):
        """S13 comes after S10 because a template can put the SECTION
        heading inside a content control."""
        builder = fx.SpecDocBuilder()
        builder.add_xml(
            fx.block_control(
                fx.paragraph("SECTION 21 13 13"),
                fx.paragraph("WET-PIPE SPRINKLER SYSTEMS"),
                tag="section-heading",
                control_id=7,
            )
        )
        builder.add_blocks(CLEAN)
        path = _write(tmp_path, "211313.docx", builder)
        heading = extract_text_from_docx(path).section_heading
        assert heading.number == "21 13 13"
        assert heading.location == "cc0p0-cc0p1"

    def test_a_page_header_is_not_the_documents_identity(self, tmp_path):
        builder = fx.build_blocks(CLEAN)
        builder.document.sections[0].header.paragraphs[0].text = "SECTION 21 13 13"
        path = _write(tmp_path, "spec.docx", builder)
        spec = extract_text_from_docx(path)
        assert "SECTION 21 13 13" in spec.content  # read, as header text
        assert spec.section_heading is None

    def test_a_document_without_a_heading_has_none(self, tmp_path):
        path = _write(tmp_path, "spec.docx", CLEAN)
        assert extract_text_from_docx(path).section_heading is None


# ===========================================================================
# 2. Routing at the assignment seam (real DOCX, real extraction)
# ===========================================================================


class TestCompactNamesAndCorroboration:
    @pytest.mark.parametrize(
        "name,blocks,module",
        [
            ("210500.docx", _heading("21 05 00", "COMMON WORK RESULTS FOR FIRE SUPPRESSION"), FIRE),
            ("211313.docx", _heading("21 13 13", "WET-PIPE SPRINKLER SYSTEMS"), FIRE),
            (
                "211313.docx",
                (fx.Para("SECTION 211313 - WET-PIPE SPRINKLER SYSTEMS", "section"), *CLEAN),
                FIRE,
            ),
            ("260500.docx", _heading("26 05 00", "COMMON WORK RESULTS FOR ELECTRICAL"), ELEC),
            ("284600.docx", _heading("28 46 00", "FIRE DETECTION AND ALARM"), ESS),
            ("099100.docx", _heading("09 91 00", "PAINTING"), ARCH),
        ],
    )
    def test_a_compact_name_confirmed_by_its_heading_routes(
        self, tmp_path, name, blocks, module
    ):
        _spec, decision = _route(tmp_path, name, blocks)
        assert decision.automatic_state is RoutingState.SUPPORTED
        assert decision.automatic_module_ids == (module,)
        heading_items = [
            item
            for item in decision.evidence
            if item.source is RoutingEvidenceSource.SECTION_HEADING
            and item.module_id == module
            and item.weight == 0.95
        ]
        assert heading_items and heading_items[0].location.startswith("p0")
        assert any(
            item.source is RoutingEvidenceSource.FILENAME
            and "confirmed by" in item.detail
            for item in decision.evidence
        )

    def test_a_compact_name_without_a_heading_stays_unsupported(self, tmp_path):
        _spec, decision = _route(tmp_path, "210500.docx", CLEAN)
        assert decision.automatic_state is RoutingState.UNSUPPORTED
        assert decision.automatic_module_ids == ()
        (note,) = decision.evidence
        assert note.source is RoutingEvidenceSource.FILENAME
        assert note.module_id is None and note.weight == 0.0
        assert "has none" in note.detail

    def test_a_compact_number_the_heading_does_not_confirm_is_ignored(self, tmp_path):
        """A mismatched compact number is not a contradiction: six leading
        digits are as often a date or a project number."""
        _spec, decision = _route(
            tmp_path, "211313.docx", _heading("26 05 00", "COMMON WORK RESULTS FOR ELECTRICAL")
        )
        assert decision.automatic_state is RoutingState.SUPPORTED
        assert decision.automatic_module_ids == (ELEC,)
        assert all(item.module_id != FIRE for item in decision.evidence)
        assert any(
            item.source is RoutingEvidenceSource.FILENAME
            and "is ignored" in item.detail
            for item in decision.evidence
        )

    def test_a_date_prefix_does_not_disturb_a_headed_document(self, tmp_path):
        _spec, decision = _route(
            tmp_path, "240105 Sprinkler Spec.docx", _heading("21 13 13", "WET-PIPE SPRINKLER SYSTEMS")
        )
        assert decision.automatic_state is RoutingState.SUPPORTED
        assert decision.automatic_module_ids == (FIRE,)

    @pytest.mark.parametrize(
        "name",
        [
            "NFPA 13 Checklist.docx",
            "2024-05-01 Addendum 2.docx",
            "Project 230415 Electrical Coordination.docx",
            "Campus Project 07 27 26 Issue 2026-07-20.docx",
        ],
    )
    def test_guard_names_grant_no_section(self, tmp_path, name):
        _spec, decision = _route(tmp_path, name, CLEAN)
        assert not any(
            item.source is RoutingEvidenceSource.FILENAME and item.weight == 0.95
            for item in decision.evidence
        )
        assert all(
            item.source is not RoutingEvidenceSource.SECTION_HEADING
            for item in decision.evidence
        )

    def test_a_headed_document_routes_whatever_its_file_is_called(self, tmp_path):
        _spec, decision = _route(
            tmp_path, "Fire Package.docx", _heading("21 13 13", "WET-PIPE SPRINKLER SYSTEMS")
        )
        assert decision.automatic_state is RoutingState.SUPPORTED
        assert decision.automatic_module_ids == (FIRE,)


def _related_sections(*lines: str) -> tuple:
    return (
        fx.Para("PART 1 GENERAL", "part"),
        fx.Para("1.01 RELATED SECTIONS", "article"),
        *(fx.Para(f"A. {line}", "body") for line in lines),
        *CLEAN[1:],
    )


class TestRelatedSectionReferences:
    def test_a_related_section_cannot_override_the_real_heading(self, tmp_path):
        blocks = fx.with_section_heading(
            _related_sections(
                "Section 21 13 13 - Wet-Pipe Sprinkler Systems.",
                "SECTION 26 05 00 - Common Work Results for Electrical",
            ),
            "09 91 00",
            "PAINTING",
        )
        spec, decision = _route(tmp_path, "099100.docx", blocks)
        assert spec.section_heading.number == "09 91 00"
        assert decision.automatic_state is RoutingState.SUPPORTED
        assert decision.automatic_module_ids == (ARCH,)
        assert {item.signal for item in decision.evidence if item.weight == 0.95} == {
            "09 91 00"
        }

    def test_a_related_section_alone_is_not_the_documents_identity(self, tmp_path):
        spec, decision = _route(
            tmp_path,
            "Fire Package.docx",
            _related_sections("SECTION 21 13 13 - WET-PIPE SPRINKLER SYSTEMS"),
        )
        assert spec.section_heading is None
        assert all(item.signal != "21 13 13" for item in decision.evidence)
        assert decision.automatic_state is not RoutingState.SUPPORTED


class TestContradictionsAreAmbiguous:
    def test_a_separated_name_and_a_different_heading(self, tmp_path):
        _spec, decision = _route(
            tmp_path,
            "21 13 13 - Wet-Pipe Sprinkler Systems.docx",
            _heading("26 05 00", "COMMON WORK RESULTS FOR ELECTRICAL"),
        )
        assert decision.automatic_state is RoutingState.AMBIGUOUS
        assert decision.module_ids == ()
        assert decision.candidate_module_ids == (FIRE, ELEC)
        assert decision.confidence == 0.50
        conflict = decision.evidence[0]  # first: the dialog shows the first lines
        assert conflict.module_id is None and conflict.weight == 0.0
        assert "26 05 00" in conflict.detail and "21 13 13" in conflict.detail
        assert "will not choose" in conflict.detail

    def test_the_decision_does_not_depend_on_which_surface_is_read_first(self):
        heading = si.SectionHeading("26 05 00", "", "SECTION 26 05 00", "p0")
        by_heading = route_spec(
            SpecRoutingInput(spec_id="x.docx", filename="21 13 13.docx", heading=heading)
        )
        by_metadata = route_spec(
            SpecRoutingInput(
                spec_id="x.docx", filename="21 13 13.docx", section_number="26 05 00"
            )
        )
        for decision in (by_heading, by_metadata):
            assert decision.automatic_state is RoutingState.AMBIGUOUS
            assert decision.candidate_module_ids == (FIRE, ELEC)

    def test_a_division_number_agrees_with_the_heading_it_prefixes(self):
        heading = si.SectionHeading("21 13 13", "", "SECTION 21 13 13", "p0")
        decision = route_spec(
            SpecRoutingInput(spec_id="x.docx", section_number="21", heading=heading)
        )
        assert decision.automatic_state is RoutingState.SUPPORTED
        assert decision.automatic_module_ids == (FIRE,)

    def test_disagreeing_numbers_with_no_candidate_are_unsupported(self, tmp_path):
        _spec, decision = _route(
            tmp_path,
            "27 13 00 - Communications Backbone Cabling.docx",
            _heading("27 15 00", "COMMUNICATIONS HORIZONTAL CABLING"),
        )
        assert decision.automatic_state is RoutingState.UNSUPPORTED
        assert decision.automatic_module_ids == ()
        assert "27 15 00" in decision.evidence[0].detail
        assert "27 13 00" in decision.evidence[0].detail

    def test_two_sections_of_one_division_still_disagree(self, tmp_path):
        _spec, decision = _route(
            tmp_path,
            "21 13 16 - Dry-Pipe Sprinkler Systems.docx",
            _heading("21 13 13", "WET-PIPE SPRINKLER SYSTEMS"),
        )
        assert decision.automatic_state is RoutingState.AMBIGUOUS
        assert decision.candidate_module_ids == (FIRE,)

    def test_a_file_name_title_that_points_elsewhere(self, tmp_path):
        _spec, decision = _route(
            tmp_path, "Painting.docx", _heading("21 13 13", "WET-PIPE SPRINKLER SYSTEMS")
        )
        assert decision.automatic_state is RoutingState.AMBIGUOUS
        assert decision.candidate_module_ids == (FIRE, ARCH)
        assert "file name vs document" == decision.evidence[0].signal

    def test_an_unsupported_scope_named_by_the_file_is_not_silently_alarm(self, tmp_path):
        _spec, decision = _route(
            tmp_path,
            "28 13 00 Access Control.docx",
            _heading("28 46 00", "FIRE DETECTION AND ALARM"),
        )
        assert decision.automatic_state is RoutingState.AMBIGUOUS
        assert decision.candidate_module_ids == (ESS,)

    def test_agreeing_surfaces_are_not_a_contradiction(self, tmp_path):
        _spec, decision = _route(
            tmp_path,
            "21 05 00 - Common Work Results for Fire Suppression.docx",
            _heading("21 05 00", "COMMON WORK RESULTS FOR FIRE SUPPRESSION"),
        )
        assert decision.automatic_state is RoutingState.SUPPORTED
        assert decision.automatic_module_ids == (FIRE,)
        assert {"section_heading", "filename"} <= set(_sources(decision))
        # An extracted spec carries no supplied title: the file name is its
        # own surface, no longer routed as if it were a title.
        assert "section_title" not in _sources(decision)


def test_a_program_without_every_discipline_still_routes():
    """The Division 28 rules read the alarm module's title matches whatever
    modules the program holds; a smaller program must not fail on them."""
    from src.programs import ProgramDefinition

    fire_only = ProgramDefinition(
        program_id="fire_only",
        display_name="Fire only",
        description="A one-discipline program.",
        module_ids=(FIRE,),
    )
    heading = si.SectionHeading("28 31 00", "FIRE ALARM SYSTEMS", "SECTION 28 31 00", "p0", "p1")
    decision = route_spec(
        SpecRoutingInput(spec_id="283100.docx", filename="283100.docx", heading=heading),
        program=fire_only,
    )
    assert decision.automatic_state is RoutingState.UNSUPPORTED


class TestDivision27And28Unchanged:
    @pytest.mark.parametrize(
        "name,number,title",
        [
            ("271500.docx", "27 15 00", "COMMUNICATIONS HORIZONTAL CABLING"),
            ("281300.docx", "28 13 00", "ACCESS CONTROL"),
            ("282300.docx", "28 23 00", "VIDEO SURVEILLANCE"),
        ],
    )
    def test_unsupported_scopes_stay_unsupported(self, tmp_path, name, number, title):
        _spec, decision = _route(tmp_path, name, _heading(number, title))
        assert decision.automatic_state is RoutingState.UNSUPPORTED
        assert decision.module_ids == ()

    def test_legacy_28_31_is_corroborated_by_a_fire_alarm_heading_title(self, tmp_path):
        _spec, decision = _route(
            tmp_path, "283100.docx", _heading("28 31 00", "FIRE DETECTION AND ALARM")
        )
        assert decision.automatic_state is RoutingState.SUPPORTED
        assert decision.automatic_module_ids == (ESS,)

    def test_legacy_28_31_without_corroboration_still_needs_confirmation(self, tmp_path):
        _spec, decision = _route(
            tmp_path, "283100.docx", _heading("28 31 00", "GENERAL SYSTEM REQUIREMENTS")
        )
        assert decision.automatic_state is RoutingState.AMBIGUOUS
        assert decision.candidate_module_ids == (ESS,)
        assert any("corroborating" in item.detail for item in decision.evidence)

    def test_legacy_28_31_intrusion_detection_stays_unsupported(self, tmp_path):
        _spec, decision = _route(
            tmp_path, "283100.docx", _heading("28 31 00", "INTRUSION DETECTION")
        )
        assert decision.automatic_state is RoutingState.UNSUPPORTED


# ===========================================================================
# 3. Source identity: one input per file, never two files under one name
# ===========================================================================


def _two_same_named(tmp_path: Path, *, second_name: str = "spec.docx"):
    first = _write(tmp_path, "spec.docx", _heading("21 05 00", "FIRE SUPPRESSION"), folder="a")
    second = _write(
        tmp_path, second_name, _heading("26 05 00", "COMMON WORK RESULTS FOR ELECTRICAL"), folder="b"
    )
    return first, second


class TestSourceIdentity:
    def test_unique_inputs_drop_repeats_of_one_file(self, tmp_path):
        path = _write(tmp_path, "spec.docx", CLEAN, folder="a")
        (tmp_path / "b").mkdir()
        spelled_differently = tmp_path / "b" / ".." / "a" / "spec.docx"
        link = tmp_path / "b" / "spec.docx"
        try:
            link.symlink_to(path)
        except (OSError, NotImplementedError):  # pragma: no cover - no symlinks
            link = spelled_differently
        assert unique_spec_inputs([path, spelled_differently, str(path), link]) == [path]

    def test_a_spelling_through_a_missing_folder_is_the_same_file(self, tmp_path):
        """``b/../a/spec.docx`` names ``a/spec.docx`` even when ``b`` does not
        exist: resolved before its identity is taken."""
        path = _write(tmp_path, "spec.docx", CLEAN, folder="a")
        spelled = tmp_path / "missing" / ".." / "a" / "spec.docx"
        assert unique_spec_inputs([path, spelled]) == [path]

    @pytest.mark.parametrize("second_name", ["spec.docx", "SPEC.docx"])
    @pytest.mark.parametrize("reverse", [False, True])
    def test_different_files_sharing_a_name_are_refused_in_either_order(
        self, tmp_path, second_name, reverse
    ):
        first, second = _two_same_named(tmp_path, second_name=second_name)
        paths = [second, first] if reverse else [first, second]
        with pytest.raises(BasenameCollisionError) as raised:
            unique_spec_inputs(paths)
        (paths_named,) = raised.value.collisions.values()
        assert set(paths_named) == {first, second}
        assert str(first) in str(raised.value) and str(second) in str(raised.value)

    @pytest.mark.parametrize("reverse", [False, True])
    def test_routing_refuses_colliding_sources_before_routing(self, tmp_path, reverse):
        first, second = _two_same_named(tmp_path)
        specs = [extract_text_from_docx(first), extract_text_from_docx(second)]
        paths = [first, second]
        if reverse:
            specs.reverse()
            paths.reverse()
        with pytest.raises(BasenameCollisionError):
            assignments_for_specs(specs, paths, program=HYPERSCALE_DATACENTER_PROGRAM)

    def test_a_spec_is_bound_to_the_file_it_was_extracted_from(self, tmp_path):
        first, second = _two_same_named(tmp_path)
        stale = extract_text_from_docx(first)
        with pytest.raises(ValueError, match="was extracted from"):
            assignments_for_specs(
                [stale], [second], program=HYPERSCALE_DATACENTER_PROGRAM
            )
        (assignment,) = assignments_for_specs(
            [stale], [first], program=HYPERSCALE_DATACENTER_PROGRAM
        )
        assert assignment.source_path == str(first)

    def test_two_specs_with_one_name_are_refused(self, tmp_path):
        path = _write(tmp_path, "spec.docx", CLEAN)
        spec = extract_text_from_docx(path)
        with pytest.raises(ValueError, match="both named"):
            assignments_for_specs(
                [spec, spec], [path], program=HYPERSCALE_DATACENTER_PROGRAM
            )

    def test_basename_key_is_case_insensitive(self):
        assert basename_key("Spec.DOCX") == basename_key("spec.docx")


class TestHeadlessBoundaries:
    """The GUI refuses a colliding pair when files are added; these are the
    entry points that do not go through the GUI (plan WP-05 item 6)."""

    def _forbid_paid_and_extraction_work(self, monkeypatch):
        from src.orchestration import pipeline as pl
        import src.research as research

        def boom(*_args, **_kwargs):
            raise AssertionError("work ran before the collision was refused")

        monkeypatch.setattr(pl, "extract_multiple_specs_cached", boom)
        monkeypatch.setattr(research, "run_requirements_research", boom)

    @pytest.mark.parametrize("reverse", [False, True])
    def test_single_module_preparation_refuses_before_extraction(
        self, tmp_path, monkeypatch, reverse
    ):
        from src.modules import require_module
        from src.orchestration import pipeline as pl

        first, second = _two_same_named(tmp_path)
        self._forbid_paid_and_extraction_work(monkeypatch)
        files = [second, first] if reverse else [first, second]
        with pytest.raises(BasenameCollisionError):
            pl.prepare_batch_review(
                input_dir=tmp_path,
                files=files,
                module=require_module(FIRE),
            )

    def test_research_is_not_paid_for_before_the_refusal(self, tmp_path, monkeypatch):
        from src.core.project_profile import ProjectProfile
        from src.modules import require_module
        from src.orchestration import pipeline as pl

        first, second = _two_same_named(tmp_path)
        self._forbid_paid_and_extraction_work(monkeypatch)
        profile = ProjectProfile(
            city="Ashburn", state_or_province="VA", country="US", client_name="Example"
        )
        module = require_module(FIRE)
        assert pl._research_phase_applies(module, profile)
        with pytest.raises(BasenameCollisionError):
            pl.prepare_batch_review(
                input_dir=tmp_path,
                files=[first, second],
                module=module,
                project_profile=profile,
            )

    def test_a_directory_whose_names_differ_only_in_case_is_refused(
        self, tmp_path, monkeypatch
    ):
        from src.orchestration import pipeline as pl

        _write(tmp_path, "Spec.docx", CLEAN, folder="project")
        try:
            _write(tmp_path, "spec.docx", CLEAN, folder="project")
        except OSError:  # pragma: no cover - a case-insensitive volume
            pytest.skip("the filesystem cannot hold both names")
        if len(list((tmp_path / "project").iterdir())) != 2:
            pytest.skip("the filesystem is case-insensitive")
        self._forbid_paid_and_extraction_work(monkeypatch)
        with pytest.raises(BasenameCollisionError):
            pl._prepare_specs(input_dir=tmp_path / "project")

    def test_a_repeated_file_is_reviewed_once(self, tmp_path):
        from src.orchestration import pipeline as pl

        path = _write(tmp_path, "210500.docx", _heading("21 05 00", "FIRE SUPPRESSION"))
        prepared = pl._prepare_specs(
            input_dir=tmp_path, files=[path, path], preflight=False
        )
        assert [spec.filename for spec in prepared.specs] == ["210500.docx"]

    def test_a_resume_whose_saved_inputs_collide_keeps_its_paid_results(self, tmp_path):
        """A record written before the guard existed may name two files that
        share a name. Resume must not fail on it: the review results come
        back from the saved request map, and only re-extraction is skipped,
        as for a missing file."""
        from src.modules import require_module
        from src.orchestration import pipeline as pl

        first, second = _two_same_named(tmp_path)
        request_map = {"review__0": {"filename": "spec.docx", "index": 0, "type": "review"}}
        logs: list[tuple[str, str]] = []
        submission = pl.reconstruct_batch_submission(
            batch_id="msgbatch_saved",
            request_map=request_map,
            review_request_ids=["review__0"],
            files_reviewed=["spec.docx"],
            input_dir=str(tmp_path),
            files=[str(first), str(second)],
            model="test-model",
            project_context="",
            module=require_module(FIRE),
            cross_check_enabled=False,
            created_at=1.0,
            log=lambda message, level="info": logs.append((level, message)),
        )
        assert submission.job.request_map == request_map
        assert submission.prepared_specs is None
        assert any(
            level == "warning" and "share a file name" in message
            for level, message in logs
        )

    def _program_assignment(self, source_path: str, module_id: str) -> SpecAssignment:
        from src.programs import SpecRoutingDecision

        return SpecAssignment(
            source_path=source_path,
            decision=SpecRoutingDecision(
                spec_id=Path(source_path).name,
                program_id=HYPERSCALE_DATACENTER_PROGRAM.program_id,
                automatic_state=RoutingState.SUPPORTED,
                automatic_module_ids=(module_id,),
                confidence=0.95,
                evidence=(),
            ),
        )

    @pytest.mark.parametrize("second_name", ["spec.docx", "Spec.docx"])
    @pytest.mark.parametrize("reverse", [False, True])
    def test_program_preparation_refuses_across_partitions_before_any_module(
        self, monkeypatch, second_name, reverse
    ):
        """Each module's partition looks unique on its own; only the whole
        program's inputs show the collision."""
        from src.orchestration import program_pipeline as pp

        calls: list[str] = []
        monkeypatch.setattr(
            pp, "prepare_batch_review", lambda **kwargs: calls.append("prepare")
        )
        assignments = [
            self._program_assignment("C:/a/spec.docx", FIRE),
            self._program_assignment(f"C:/b/{second_name}", ELEC),
        ]
        if reverse:
            assignments.reverse()
        with pytest.raises(ValueError):
            pp.prepare_program_review(
                program_id=HYPERSCALE_DATACENTER_PROGRAM.program_id,
                assignments=assignments,
                input_dir=Path("C:/"),
                model="test-model",
            )
        assert calls == []

    def test_program_preparation_refuses_a_duplicate_before_any_module(self, monkeypatch):
        from src.orchestration import program_pipeline as pp

        calls: list[str] = []
        monkeypatch.setattr(
            pp, "prepare_batch_review", lambda **kwargs: calls.append("prepare")
        )
        twice = self._program_assignment("C:/a/spec.docx", FIRE)
        with pytest.raises(ValueError, match="Duplicate program assignment"):
            pp.prepare_program_review(
                program_id=HYPERSCALE_DATACENTER_PROGRAM.program_id,
                assignments=[twice, twice],
                input_dir=Path("C:/"),
                model="test-model",
            )
        assert calls == []


# ===========================================================================
# 4. Provenance survives saved state and resume
# ===========================================================================


class TestProvenanceSurvivesSavedState:
    def _headed_assignment(self, tmp_path: Path) -> SpecAssignment:
        path = _write(
            tmp_path, "211313.docx", (fx.Para("SECTION 211313 - WET-PIPE SPRINKLER SYSTEMS", "section"), *CLEAN)
        )
        (assignment,) = assignments_for_specs(
            [extract_text_from_docx(path)], [path], program=HYPERSCALE_DATACENTER_PROGRAM
        )
        return assignment

    def test_an_assignment_round_trips_through_json(self, tmp_path):
        assignment = self._headed_assignment(tmp_path)
        data = json.loads(json.dumps(assignment.to_dict()))
        assert SpecAssignment.from_dict(data) == assignment
        heading_items = [
            item for item in data["evidence"] if item["source"] == "section_heading"
        ]
        assert heading_items and heading_items[0]["location"] == "p0"

    def test_an_assignment_saved_before_locations_existed_still_loads(self, tmp_path):
        data = self._headed_assignment(tmp_path).to_dict()
        for item in data["evidence"]:
            item.pop("location")
        restored = SpecAssignment.from_dict(data)
        assert all(item.location == "" for item in restored.decision.evidence)
        assert restored.module_ids == (FIRE,)

    def test_a_program_manifest_resumes_with_the_same_routing(self, tmp_path):
        from src.batch.batch import BatchJob
        from src.modules import require_module
        from src.orchestration import program_pipeline as pp
        from src.orchestration.batch_resume import (
            PendingProgramRun,
            load_pending_run,
            save_pending_program_run,
        )
        from src.orchestration.pipeline import BatchSubmission

        assignment = self._headed_assignment(tmp_path)
        module = require_module(FIRE)
        child = BatchSubmission(
            job=BatchJob(
                batch_id="msgbatch_fire",
                job_type="review",
                request_map={"review__0": {"filename": "211313.docx", "index": 0, "type": "review"}},
                created_at=1_700_000_000.0,
            ),
            files_reviewed=["211313.docx"],
            review_request_ids=["review__0"],
            model="test-model",
            project_context="",
            cycle_label=module.cycle.label,
            module_id=FIRE,
        )
        submission = pp.ProgramSubmission(
            program_id=HYPERSCALE_DATACENTER_PROGRAM.program_id,
            assignments=(assignment,),
            partitions={FIRE: child},
        )
        state = tmp_path / "pending.json"
        assert save_pending_program_run(
            PendingProgramRun.from_submission(submission), path=state
        )
        loaded = load_pending_run(path=state)
        assert isinstance(loaded, PendingProgramRun)
        resumed = loaded.to_submission()
        assert resumed.assignments == (assignment,)
        (restored,) = resumed.assignments
        assert restored.source_path == assignment.source_path
        assert any(
            item.source is RoutingEvidenceSource.SECTION_HEADING and item.location == "p0"
            for item in restored.decision.evidence
        )
