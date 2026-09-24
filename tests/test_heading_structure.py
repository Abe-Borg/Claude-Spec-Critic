"""Section structure for the empty-section and duplicate-heading checks.

Plan WP-04A (chunk S03). The preprocessor used to read headings flat: any
paragraph starting with a number was a heading, and a heading was "empty"
when the next heading came straight after it. So every PART heading in a
clean specification was flagged empty (its first article follows it
directly), and quantity lines such as "2 coats of primer shall be applied."
were headings, which also made a repeated quantity line a "duplicate
heading".

``heading_candidates`` now qualifies each heading (number AND title shape),
a heading's content is its whole subtree, and an empty PART is reported once
instead of once per empty article. These tests pin that behavior, the
unchanged alert contract (rule ids, text, positions, order, limits), and the
shared DOCX fixtures end to end through extraction.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core.code_cycles import CALIFORNIA_2025
from src.input.extractor import extract_text_from_docx
from src.input.preprocessor import (
    DETERMINISTIC_RULE_DUPLICATE_HEADING,
    DETERMINISTIC_RULE_EMPTY_SECTION,
    HEADING_PROVENANCE_TYPED,
    HeadingCandidate,
    detect_duplicate_headings,
    detect_empty_sections,
    heading_candidates,
    preprocess_spec,
)
from tests.fixtures import spec_docx as fx


def _doc(*paragraphs: str) -> str:
    """Paragraphs joined the way the extractor joins them."""
    return "\n\n".join(paragraphs)


def _empty(content: str) -> list[str]:
    return [alert["match"] for alert in detect_empty_sections(content, "s.docx")]


def _duplicates(content: str) -> list[str]:
    return [alert["match"] for alert in detect_duplicate_headings(content, "s.docx")]


def _numbers(content: str) -> list[str]:
    return [heading.number for heading in heading_candidates(content)]


# ---------------------------------------------------------------------------
# Which lines are headings
# ---------------------------------------------------------------------------


class TestHeadingCandidates:
    def test_a_candidate_carries_number_title_level_position_and_provenance(self):
        content = _doc("PART 1 GENERAL", "1.01 SUMMARY", "A. Provide piping.")
        part, article = heading_candidates(content)
        assert part == HeadingCandidate(
            number="PART 1",
            title="GENERAL",
            level=0,
            start=0,
            end=len("PART 1 GENERAL"),
            provenance=HEADING_PROVENANCE_TYPED,
        )
        assert (article.number, article.title, article.level) == ("1.01", "SUMMARY", 1)
        assert article.start == content.index("1.01")
        assert article.label == "1.01 SUMMARY"

    @pytest.mark.parametrize(
        "line, number, title, level",
        [
            ("PART 1 GENERAL", "PART 1", "GENERAL", 0),
            ("PART 1 - GENERAL", "PART 1", "- GENERAL", 0),
            ("Part 2 Products", "PART 2", "Products", 0),
            ("PART  3   EXECUTION", "PART 3", "EXECUTION", 0),
            ("1.01 SUMMARY", "1.01", "SUMMARY", 1),
            ("1.1 Summary", "1.1", "Summary", 1),
            ("1.04 Delivery, Storage, and Handling", "1.04", "Delivery, Storage, and Handling", 1),
            ("1.02 SUBMITTALS:", "1.02", "SUBMITTALS", 1),
            ("1.01 SUMMARY.", "1.01", "SUMMARY.", 1),
            ("2.3.1 PIPE HANGERS", "2.3.1", "PIPE HANGERS", 2),
        ],
    )
    def test_heading_shapes_are_accepted(self, line, number, title, level):
        (heading,) = heading_candidates(line)
        assert (heading.number, heading.title, heading.level) == (number, title, level)

    @pytest.mark.parametrize(
        "line",
        [
            # Plan WP-04A's integer-led prose: quantities, not headings.
            "2 coats of primer shall be applied.",
            "12 inches minimum clearance shall be maintained.",
            "1 year from Substantial Completion.",
            # A bare integer is never a heading number, even when the rest
            # of the line looks like a title: a SectionFormat PART heading
            # always carries the word PART.
            "3 EXECUTION",
            # Dotted numbers are not accepted blindly: the title must read as
            # a title. Each of these continues or completes a sentence.
            "1.5 inches minimum cover is required.",
            "1.5 Inches minimum cover is required.",
            "1.5 INCHES OF COVER SHALL BE PROVIDED",
            "2.5 MUST BE VERIFIED IN THE FIELD",
            "1.1 Summary.",
            "0.75 inch minimum wall thickness",
            # A table row (the extractor joins cells with " | ").
            "2.01 | MATERIALS",
            "2.01 MATERIALS | Copper tube",
            # No title at all.
            "1.01 1234",
            "1.01 :",
        ],
    )
    def test_prose_and_quantities_are_not_headings(self, line):
        assert heading_candidates(line) == []

    def test_a_heading_starts_a_paragraph(self):
        content = _doc("A. See 1.01 SUMMARY for scope.", "Refer to PART 2 PRODUCTS.")
        assert heading_candidates(content) == []

    def test_a_title_over_120_characters_is_prose(self):
        long_title = "SUMMARY " + "X" * 120
        assert heading_candidates(f"1.01 {long_title}") == []
        assert _numbers(f"1.01 {'X' * 120}") == ["1.01"]

    def test_run_in_text_after_a_colon_is_the_headings_own_content(self):
        run_in, bare = heading_candidates(_doc("1.03 REFERENCES: ASTM A53", "1.04 SUBMITTALS:"))
        assert run_in.run_in and run_in.title == "REFERENCES: ASTM A53"
        assert not bare.run_in and bare.title == "SUBMITTALS"

    def test_a_line_break_inside_the_paragraph_ends_the_heading_line(self):
        content = "1.01 SUMMARY\nA. Provide piping."
        (heading,) = heading_candidates(content)
        assert heading.title == "SUMMARY"
        assert heading.end == content.index("\n")

    def test_every_heading_check_reads_the_same_candidates(self):
        # The quantity line is neither a heading for the empty check nor a
        # repeated heading for the duplicate check.
        quantity = "2 coats of primer shall be applied."
        content = _doc("1.01 PAINTING", quantity, "1.02 CLEANING", quantity)
        assert _numbers(content) == ["1.01", "1.02"]
        assert _empty(content) == []
        assert _duplicates(content) == []


# ---------------------------------------------------------------------------
# Empty sections
# ---------------------------------------------------------------------------


class TestEmptySections:
    def test_a_part_with_populated_articles_is_not_empty(self):
        content = _doc("PART 1 GENERAL", "1.01 SUMMARY", "A. Provide piping.")
        assert _empty(content) == []

    def test_a_truly_empty_leaf_article_is_reported(self):
        content = _doc(
            "PART 1 GENERAL", "1.01 SUMMARY", "A. Provide piping.", "1.02 SUBMITTALS",
            "PART 2 PRODUCTS", "2.01 MATERIALS", "A. Copper.",
        )
        assert _empty(content) == ["1.02 SUBMITTALS"]

    def test_an_article_whose_only_body_is_a_table_row_is_not_empty(self):
        content = _doc("2.01 MATERIALS", "Piping | Copper tube, Type L", "2.02 FITTINGS", "A. Wrought.")
        assert _empty(content) == []

    def test_an_empty_part_with_no_articles_is_reported(self):
        content = _doc("PART 1 GENERAL", "1.01 SUMMARY", "A. Provide piping.", "PART 2 PRODUCTS")
        assert _empty(content) == ["PART 2 PRODUCTS"]

    def test_an_empty_part_is_reported_once_not_once_per_empty_article(self):
        # Nonredundant ancestor/leaf policy: every heading under PART 2 is
        # empty, so the PART is the one alert and its articles add none.
        content = _doc(
            "PART 1 GENERAL", "1.01 SUMMARY", "A. Provide piping.",
            "PART 2 PRODUCTS", "2.01 MATERIALS", "2.02 FITTINGS",
            "PART 3 EXECUTION", "3.01 INSTALLATION", "A. Install.",
        )
        assert _empty(content) == ["PART 2 PRODUCTS"]

    def test_an_empty_article_is_reported_when_its_part_has_other_content(self):
        content = _doc(
            "PART 2 PRODUCTS", "2.01 MATERIALS", "2.02 FITTINGS", "A. Wrought copper.",
        )
        assert _empty(content) == ["2.01 MATERIALS"]

    def test_the_policy_applies_at_every_level(self):
        # 1.01 has content only through 1.01.2, so 1.01 is not empty and the
        # empty 1.01.1 is reported; 1.02 and its only sub-article are both
        # empty, so only 1.02 is reported.
        content = _doc(
            "1.01 SUMMARY", "1.01.1 SCOPE", "1.01.2 RELATED WORK", "A. Section 23 05 00.",
            "1.02 SUBMITTALS", "1.02.1 PRODUCT DATA",
        )
        assert _empty(content) == ["1.01.1 SCOPE", "1.02 SUBMITTALS"]

    def test_body_text_directly_under_a_part_counts(self):
        content = _doc("PART 1 GENERAL", "A. General requirements apply.", "1.01 SUMMARY", "A. Scope.")
        assert _empty(content) == []

    def test_a_run_in_heading_is_not_empty(self):
        content = _doc("1.03 REFERENCES: ASTM A53", "1.04 SUBMITTALS", "A. Submit data.")
        assert _empty(content) == []

    @pytest.mark.parametrize(
        "line",
        [
            "2 coats of primer shall be applied.",
            "12 inches minimum clearance shall be maintained.",
            "1.5 inches minimum cover is required.",
            "1 year from Substantial Completion.",
        ],
    )
    def test_a_quantity_line_is_body_text(self, line):
        content = _doc("3.01 PAINTING", line, "3.02 CLEANING", "A. Clean.")
        assert _empty(content) == []

    def test_end_of_section_closes_the_last_article(self):
        empty_last = _doc("3.01 INSTALLATION", "A. Install.", "3.02 CLEANING", "END OF SECTION 23 05 00")
        full_last = _doc("3.01 INSTALLATION", "A. Install.", "3.02 CLEANING", "A. Clean.", "END OF SECTION")
        assert _empty(empty_last) == ["3.02 CLEANING"]
        assert _empty(full_last) == []

    def test_headings_after_end_of_section_are_read_afresh(self):
        # Two sections in one file: the second section's PART has content,
        # and the END OF SECTION line is not content of the first one's
        # last article.
        content = _doc(
            "PART 3 EXECUTION", "3.01 INSTALLATION", "END OF SECTION",
            "SECTION 23 05 13", "PART 1 GENERAL", "1.01 SUMMARY", "A. Scope.",
        )
        assert _empty(content) == ["PART 3 EXECUTION"]

    @pytest.mark.parametrize(
        "delimiter",
        [
            "===== FOOTNOTE CONTENT =====",
            "===== ENDNOTE CONTENT =====",
            "===== HEADER/FOOTER CONTENT =====",
        ],
    )
    def test_supplemental_blocks_are_never_a_headings_content(self, delimiter):
        content = _doc("3.01 INSTALLATION", "A. Install.", "3.02 CLEANING", delimiter, "[Header] Project")
        assert _empty(content) == ["3.02 CLEANING"]

    def test_a_text_box_block_can_hold_the_last_headings_content(self):
        # A text box is anchored in the body, so its text may be the only
        # content of the heading it sits under; its block does not end the
        # structure (the three blocks above do).
        content = _doc(
            "3.02 SCHEDULE", "===== TEXT BOX CONTENT =====", "[Text Box] Fan schedule",
        )
        assert _empty(content) == []

    def test_the_alert_contract_is_unchanged(self):
        content = _doc("PART 1 GENERAL", "1.01 SUMMARY", "1.02 SUBMITTALS", "A. Submit.")
        (alert,) = detect_empty_sections(content, "spec.docx")
        start = content.index("1.01")
        end = content.index("1.02")
        assert alert == {
            "filename": "spec.docx",
            "type": "Empty section",
            "match": "1.01 SUMMARY",
            "context": content[max(0, start - 40):end + 40].replace("\n", " ").strip(),
            "position": start,
            "section_number": "1.01",
            "section_title": "SUMMARY",
            "deterministic_rule": DETERMINISTIC_RULE_EMPTY_SECTION,
        }

    def test_alerts_are_in_document_order_and_limited(self):
        headings = [f"1.{n:02d} ARTICLE {n}" for n in range(1, 8)]
        content = _doc("PART 1 GENERAL", "A. Scope.", *headings)
        assert _empty(content) == headings
        limited = detect_empty_sections(content, "s.docx", max_matches=3)
        assert [alert["match"] for alert in limited] == headings[:3]


# ---------------------------------------------------------------------------
# Duplicate headings
# ---------------------------------------------------------------------------


class TestDuplicateHeadings:
    def test_a_repeated_article_number_is_reported_after_its_first_occurrence(self):
        content = _doc("1.01 SUMMARY", "A. One.", "1.02 SUBMITTALS", "A. Two.", "1.01 SUMMARY", "A. Three.")
        (alert,) = detect_duplicate_headings(content, "spec.docx")
        position = content.rindex("1.01")
        assert alert == {
            "filename": "spec.docx",
            "type": "Duplicate section heading",
            "match": "1.01 SUMMARY",
            "context": content[max(0, position - 60):position + 120].replace("\n", " ").strip(),
            "position": position,
            "section_number": "1.01",
            "occurrence_count": 2,
            "deterministic_rule": DETERMINISTIC_RULE_DUPLICATE_HEADING,
        }

    def test_a_repeated_number_with_a_different_title_is_a_duplicate(self):
        content = _doc("1.02 REFERENCES", "A. One.", "1.02 SUBMITTALS", "A. Two.")
        assert _duplicates(content) == ["1.02 SUBMITTALS"]

    def test_part_numbers_are_normalized_before_comparison(self):
        content = _doc("PART 1 GENERAL", "A. One.", "Part  1 General", "A. Two.")
        assert _duplicates(content) == ["PART 1 General"]

    def test_repeated_quantity_lines_are_not_duplicate_headings(self):
        line = "12 inches minimum clearance shall be maintained."
        content = _doc("1.01 SUMMARY", line, "A. Text.", line, "B. Text.")
        assert _duplicates(content) == []

    def test_order_is_grouped_by_number_in_first_seen_order(self):
        content = _doc(
            "1.02 A", "x.", "1.01 B", "x.", "1.02 C", "x.", "1.01 D", "x.", "1.02 E", "x.",
        )
        assert _duplicates(content) == ["1.02 C", "1.02 E", "1.01 D"]
        limited = detect_duplicate_headings(content, "s.docx", max_matches=2)
        assert [alert["match"] for alert in limited] == ["1.02 C", "1.02 E"]


# ---------------------------------------------------------------------------
# The shared DOCX fixtures, end to end through extraction
# ---------------------------------------------------------------------------


def _structural(variant: fx.SpecVariant, tmp_path: Path) -> list[tuple[str, str]]:
    path = fx.save_docx(fx.build_blocks(variant.blocks), tmp_path, f"{variant.name}.docx")
    spec = extract_text_from_docx(path)
    result = preprocess_spec(spec.content, spec.filename, cycle=CALIFORNIA_2025)
    return [(alert["deterministic_rule"], alert["match"]) for alert in result.structural_alerts]


class TestThreePartFixtures:
    @pytest.mark.parametrize(
        "variant", [v for v in fx.three_part_variants() if v.clean], ids=lambda v: v.name
    )
    def test_clean_variants_raise_no_structural_alert(self, variant, tmp_path):
        assert _structural(variant, tmp_path) == []

    @pytest.mark.parametrize(
        "variant", [v for v in fx.three_part_variants() if not v.clean], ids=lambda v: v.name
    )
    def test_each_mutation_raises_only_its_own_alert(self, variant, tmp_path):
        assert _structural(variant, tmp_path) == [(variant.expected_rule, variant.expected_match)]

    def test_structural_alerts_list_empty_sections_before_duplicates(self):
        content = _doc("1.01 SUMMARY", "A. One.", "1.01 SUMMARY", "1.02 SUBMITTALS", "A. Two.")
        result = preprocess_spec(content, "s.docx", cycle=CALIFORNIA_2025)
        assert [(a["deterministic_rule"], a["match"]) for a in result.structural_alerts] == [
            (DETERMINISTIC_RULE_EMPTY_SECTION, "1.01 SUMMARY"),
            (DETERMINISTIC_RULE_DUPLICATE_HEADING, "1.01 SUMMARY"),
        ]
