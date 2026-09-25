"""Display groups vs executable occurrences (plan WP-06B, chunks S11 and S12).

A display group is one finding as the report shows it; an executable
occurrence is one place its edit applies — one file, one element, one
instruction. Before S11 ``group_findings`` keyed occurrences by *file*, bound
each file to its first recorded original, and numbered them with presentation
counters (``grp-0000::000::a.docx``), so the same fix at p4 and p8 of one file
reached the sidecar once and an occurrence's id changed with input order.

These tests pin the model that replaced it:

* one occurrence per file and target, where the target is the element the
  review named — validated against the reviewed text when it is available —
  plus the instruction, compared exactly; genuine duplicate emissions
  collapse, and two instructions the dedup key cannot tell apart ("Bar" and
  "bar") do not;
* members that name no usable element are one *uncertain* occurrence per file
  and instruction, never several, and never absorbed into a located one;
* a file with no recorded original is ``missing_original`` and borrows no
  other location's element or anchor;
* occurrence ids are content-derived, carry module identity, and do not move
  with input order or presentation counters;
* the sidecar writer emits one entry per occurrence (schemas 6 and 7, S12),
  pinned by a golden beside the legacy one it replaced, which the applier
  still reads as before.
"""
from __future__ import annotations

import itertools
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from docx import Document

from src.input.extractor import extract_text_from_docx
from src.orchestration import pipeline
from src.orchestration.pipeline import (
    LOCATION_CLAIMED,
    LOCATION_MISSING_ORIGINAL,
    LOCATION_UNRESOLVED,
    LOCATION_VALIDATED,
    _deduplicate_findings,
    compute_occurrence_id,
    edit_occurrences,
    element_index_from_specs,
    group_findings,
)
from src.output import edit_sidecar
from src.review.reviewer import Finding, ReviewResult

_GOLDEN = Path(__file__).parent / "goldens" / "legacy_sidecar_payloads.json"


def finding(file_name="a.docx", element_id="p4", **overrides) -> Finding:
    fields = dict(
        severity="HIGH",
        fileName=file_name,
        section="2.01",
        issue="Gate valves are specified where ball valves are required.",
        actionType="EDIT",
        existingText="gate valve",
        replacementText="ball valve",
        codeReference=None,
        evidenceElementId=element_id,
    )
    fields.update(overrides)
    return Finding(**fields)


def addition(file_name="a.docx", element_id="p4", **overrides) -> Finding:
    fields = dict(
        actionType="ADD",
        existingText=None,
        replacementText="Provide tamper switches on every valve.",
        anchorText="Provide gate valves",
        insertPosition="after",
    )
    fields.update(overrides)
    return finding(file_name, element_id, **fields)


def merged(*members: Finding) -> Finding:
    (group,) = _deduplicate_findings(list(members))
    return group


def occurrences(*members: Finding, **kwargs):
    (group,) = group_findings([merged(*members)], **kwargs)
    return group.occurrences


def targets(occs) -> list:
    return [(o.file_name, o.element_id, o.location) for o in occs]


# ---------------------------------------------------------------------------
# One occurrence per validated target
# ---------------------------------------------------------------------------


class TestOneOccurrencePerTarget:
    def test_the_same_fix_at_p4_and_p8_of_one_file_is_two_occurrences(self):
        occs = occurrences(finding(element_id="p4"), finding(element_id="p8"))
        assert targets(occs) == [
            ("a.docx", "p4", LOCATION_CLAIMED),
            ("a.docx", "p8", LOCATION_CLAIMED),
        ]
        assert [o.original_finding.evidenceElementId for o in occs] == ["p4", "p8"]
        assert len({o.occurrence_id for o in occs}) == 2

    def test_a_duplicate_emission_at_p4_is_one_occurrence_with_both_members(self):
        first, second = finding(element_id="p4"), finding(element_id="p4")
        (only,) = occurrences(first, second)
        assert only.element_id == "p4"
        assert len(only.members) == 2
        assert {id(m) for m in only.members} == {id(first), id(second)}

    def test_duplicates_and_distinct_places_together(self):
        occs = occurrences(
            finding(element_id="p8"), finding(element_id="p4"), finding(element_id="p8")
        )
        assert [(o.element_id, len(o.members)) for o in occs] == [("p4", 1), ("p8", 2)]

    def test_two_files_with_two_places_each_are_four_occurrences(self):
        occs = occurrences(
            finding("a.docx", "p4"),
            finding("a.docx", "p8"),
            finding("b.docx", "p2"),
            finding("b.docx", "p9"),
        )
        assert sorted(targets(occs)) == [
            ("a.docx", "p4", LOCATION_CLAIMED),
            ("a.docx", "p8", LOCATION_CLAIMED),
            ("b.docx", "p2", LOCATION_CLAIMED),
            ("b.docx", "p9", LOCATION_CLAIMED),
        ]
        for occ in occs:
            assert occ.original_finding.fileName == occ.file_name
            assert occ.executable_proposal().target_element_id == occ.element_id

    def test_a_cross_file_group_still_has_one_occurrence_per_file(self):
        occs = occurrences(finding("a.docx", "p4"), finding("b.docx", "p7"), finding("c.docx", "p2"))
        # Files keep the group's ``affected_files`` order.
        assert targets(occs) == [
            ("a.docx", "p4", LOCATION_CLAIMED),
            ("b.docx", "p7", LOCATION_CLAIMED),
            ("c.docx", "p2", LOCATION_CLAIMED),
        ]

    def test_located_occurrences_sort_by_element_number_then_the_uncertain_one(self):
        occs = occurrences(
            finding(element_id=None), finding(element_id="p10"), finding(element_id="p9")
        )
        assert [(o.element_id, o.location) for o in occs] == [
            ("p9", LOCATION_CLAIMED),
            ("p10", LOCATION_CLAIMED),
            (None, LOCATION_UNRESOLVED),
        ]

    def test_the_target_element_is_the_proposals_before_the_evidence(self):
        member = finding(element_id="p4")
        member.edit_proposal = member.as_edit_proposal()
        member.edit_proposal.target_element_id = "p5"
        (only,) = occurrences(member)
        assert only.element_id == "p5"


class TestInstructionIdentity:
    """What separates two instructions at one element: the action, the text,
    and an addition's side and anchor. The text is compared exactly, as the
    applier compares it when it settles two instructions for one place
    (``applier.conflicts``), never with the case and whitespace folding the
    group's key uses: two instructions that key cannot tell apart still write
    different documents."""

    def test_additions_before_and_after_one_element_are_two_occurrences(self):
        occs = occurrences(
            addition(element_id="p4", insertPosition="before"),
            addition(element_id="p4", insertPosition="after"),
        )
        assert sorted(o.executable_proposal().insert_position for o in occs) == ["after", "before"]
        assert {o.element_id for o in occs} == {"p4"}

    def test_the_same_addition_at_one_element_is_one_occurrence(self):
        (only,) = occurrences(addition(element_id="p4"), addition(element_id="p4"))
        assert len(only.members) == 2

    def test_additions_anchored_in_two_cells_of_one_row_are_two_occurrences(self):
        """A table row is one element holding a paragraph per cell, and an
        addition's anchor chooses the paragraph it goes beside. Leaving the
        anchor out of a located target merged these into one occurrence, so
        one cell's addition never reached the sidecar."""
        occs = occurrences(
            addition(element_id="t0r1", anchorText="Gate valve, bronze"),
            addition(element_id="t0r1", anchorText="Check valve, swing"),
        )
        assert [(o.element_id, o.location) for o in occs] == [
            ("t0r1", LOCATION_CLAIMED),
            ("t0r1", LOCATION_CLAIMED),
        ]
        assert sorted(o.executable_proposal().anchor_text for o in occs) == [
            "Check valve, swing",
            "Gate valve, bronze",
        ]
        assert len({o.occurrence_id for o in occs}) == 2

    def test_a_located_addition_keeps_its_anchor(self):
        """Two anchors in one paragraph are kept apart too: from the element
        id alone, a paragraph and a row look the same. The applier, which sees
        the document, finds they go beside one paragraph and writes the new
        paragraph once, the other a ``DUPLICATE``."""
        occs = occurrences(
            addition(element_id="p4", anchorText="Provide gate valves"),
            addition(element_id="p4", anchorText="gate valves at each branch"),
        )
        assert {o.element_id for o in occs} == {"p4"}
        assert sorted(o.executable_proposal().anchor_text for o in occs) == [
            "Provide gate valves",
            "gate valves at each branch",
        ]

    def test_one_anchor_in_different_case_is_two_instructions(self):
        """The applier never matches an anchor case-insensitively. Folded into
        one occurrence, content order chose which anchor reached the sidecar,
        and the upper-case one sorts first and matches nothing."""
        occs = occurrences(
            addition(element_id="p4", anchorText="Provide gate valves"),
            addition(element_id="p4", anchorText="PROVIDE GATE VALVES"),
        )
        assert sorted(o.executable_proposal().anchor_text for o in occs) == [
            "PROVIDE GATE VALVES",
            "Provide gate valves",
        ]
        assert len({o.occurrence_id for o in occs}) == 2

    def test_replacements_differing_only_in_case_are_two_instructions(self):
        """Found in review (Codex, P1). The group's key folds case, so both
        emissions are one finding; folding them again here kept the first in
        content order and dropped the other, so the applier never saw that
        they disagree. Both reach it now, whatever the input order, and it
        holds them (``EDIT_CONFLICT``)."""

        def emissions():
            return [
                finding(element_id="p4", replacementText="Ball valve"),
                finding(element_id="p4", replacementText="ball valve"),
            ]

        seen = []
        for order in (emissions(), emissions()[::-1]):
            occs = occurrences(*order)
            assert [o.element_id for o in occs] == ["p4", "p4"]
            seen.append({(o.occurrence_id, o.executable_proposal().replacement_text) for o in occs})
        assert seen[0] == seen[1]
        assert {text for _, text in seen[0]} == {"Ball valve", "ball valve"}

    @pytest.mark.parametrize("first", ["Gate valve", "gate valve"])
    def test_case_variants_of_the_consumed_text_are_two_instructions(self, first):
        """They consume different text, so each is its own instruction. Folded,
        the first in content order stood for both, and the other never
        reached the sidecar."""
        second = "gate valve" if first == "Gate valve" else "Gate valve"
        occs = occurrences(
            finding(element_id="p4", existingText=first),
            finding(element_id="p4", existingText=second),
        )
        assert sorted(o.executable_proposal().existing_text for o in occs) == [
            "Gate valve",
            "gate valve",
        ]

    def test_surrounding_whitespace_makes_another_instruction(self):
        occs = occurrences(
            finding(element_id="p4", existingText="gate valve"),
            finding(element_id="p4", existingText="gate valve "),
        )
        assert sorted(o.executable_proposal().existing_text for o in occs) == [
            "gate valve",
            "gate valve ",
        ]

    def test_the_instruction_key_is_exact_and_about_the_instruction_alone(self):
        key = pipeline._instruction_key
        edit = finding().as_edit_proposal()
        assert key(edit) == key(replace(edit))
        # Where it points and how sure the review was are not the instruction.
        assert key(edit) == key(replace(edit, target_element_id="p9", edit_confidence=0.1))
        for changed in (
            {"existing_text": "Gate valve"},
            {"existing_text": " gate valve"},
            {"replacement_text": "Ball valve"},
            {"replacement_text": "ball valve\n"},
            {"action_type": "DELETE"},
        ):
            assert key(replace(edit, **changed)) != key(edit), changed
        add = addition().as_edit_proposal()
        for changed in (
            {"anchor_text": add.anchor_text.upper()},
            {"anchor_text": add.anchor_text + " "},
            {"insert_position": "before"},
            {"replacement_text": add.replacement_text.lower()},
        ):
            assert key(replace(add, **changed)) != key(add), changed

    def test_unlocated_additions_on_different_anchors_are_two_uncertain_places(self):
        occs = occurrences(
            addition(element_id=None, anchorText="Provide gate valves"),
            addition(element_id=None, anchorText="Install check valves"),
        )
        assert [o.location for o in occs] == [LOCATION_UNRESOLVED, LOCATION_UNRESOLVED]
        assert len({o.occurrence_id for o in occs}) == 2

    def test_unlocated_additions_on_one_anchor_are_one_uncertain_place(self):
        (only,) = occurrences(addition(element_id=None), addition(element_id=None))
        assert only.location == LOCATION_UNRESOLVED
        assert len(only.members) == 2


# ---------------------------------------------------------------------------
# Uncertainty is preserved, never multiplied
# ---------------------------------------------------------------------------


class TestIndistinguishableAnchors:
    def test_members_naming_no_element_are_one_uncertain_occurrence(self):
        (only,) = occurrences(finding(element_id=None), finding(element_id=None))
        assert only.location == LOCATION_UNRESOLVED
        assert only.element_id is None
        assert len(only.members) == 2
        assert "named no element" in only.location_note
        assert only.executable_proposal().target_element_id is None

    def test_an_unlocated_member_beside_a_located_one_stays_uncertain(self):
        """Identical prose does not prove an identical location: the id-less
        member may be p4 or another place, so it is neither folded into p4 nor
        turned into a second located instruction."""
        occs = occurrences(finding(element_id="p4"), finding(element_id=None))
        assert [(o.element_id, o.location) for o in occs] == [
            ("p4", LOCATION_CLAIMED),
            (None, LOCATION_UNRESOLVED),
        ]


# ---------------------------------------------------------------------------
# Validation against the reviewed text
# ---------------------------------------------------------------------------


_INDEX = {
    "a.docx": {
        "p4": "Provide gate valve at each branch.",
        "p8": "Unrelated clause.",
        "p9": "Provide  gate\nvalve on risers.",
        "t0r1": "Valve | gate valve",
    }
}


class TestValidation:
    def test_an_element_that_contains_the_text_is_validated(self):
        (only,) = occurrences(finding(element_id="p4"), element_index=_INDEX)
        assert (only.element_id, only.location) == ("p4", LOCATION_VALIDATED)

    def test_whitespace_shape_is_forgiven_as_the_applier_forgives_it(self):
        (only,) = occurrences(finding(element_id="p9"), element_index=_INDEX)
        assert only.location == LOCATION_VALIDATED

    def test_case_is_not_forgiven(self):
        (only,) = occurrences(
            finding(element_id="p4", existingText="Gate Valve"), element_index=_INDEX
        )
        assert only.location == LOCATION_UNRESOLVED

    def test_an_element_without_the_text_is_not_a_location(self):
        (only,) = occurrences(finding(element_id="p8"), element_index=_INDEX)
        assert only.location == LOCATION_UNRESOLVED
        assert only.element_id is None
        assert "p8" in only.location_note and "does not contain" in only.location_note
        assert only.executable_proposal().target_element_id is None

    def test_an_id_that_is_not_an_element_is_not_a_location(self):
        (only,) = occurrences(finding(element_id="p77"), element_index=_INDEX)
        assert only.location == LOCATION_UNRESOLVED
        assert "p77" in only.location_note and "not an element" in only.location_note

    def test_a_hallucinated_second_place_does_not_become_an_instruction(self):
        """p8 does not hold the text: with the reviewed text available, the
        group is one validated place plus one uncertain member, not two
        located instructions."""
        occs = occurrences(finding(element_id="p4"), finding(element_id="p8"), element_index=_INDEX)
        assert [(o.element_id, o.location) for o in occs] == [
            ("p4", LOCATION_VALIDATED),
            (None, LOCATION_UNRESOLVED),
        ]

    def test_two_real_places_are_two_validated_occurrences(self):
        occs = occurrences(finding(element_id="p4"), finding(element_id="p9"), element_index=_INDEX)
        assert [(o.element_id, o.location) for o in occs] == [
            ("p4", LOCATION_VALIDATED),
            ("p9", LOCATION_VALIDATED),
        ]

    def test_a_file_the_index_does_not_hold_keeps_claimed_ids(self):
        (only,) = occurrences(finding("b.docx", "p4"), element_index=_INDEX)
        assert only.location == LOCATION_CLAIMED

    def test_an_addition_validates_on_its_anchor(self):
        (good,) = occurrences(
            addition(element_id="p4", anchorText="Provide gate valve"), element_index=_INDEX
        )
        (bad,) = occurrences(
            addition(element_id="p4", anchorText="Install check valve"), element_index=_INDEX
        )
        assert good.location == LOCATION_VALIDATED
        assert bad.location == LOCATION_UNRESOLVED

    def test_an_addition_positioned_by_element_alone_needs_only_the_element(self):
        (only,) = occurrences(addition(element_id="p8", anchorText=None), element_index=_INDEX)
        assert only.location == LOCATION_VALIDATED

    def test_a_report_only_finding_needs_only_the_element(self):
        report_only = dict(actionType="REPORT_ONLY", existingText=None, replacementText=None)
        (only,) = occurrences(finding(element_id="p8", **report_only), element_index=_INDEX)
        assert only.location == LOCATION_VALIDATED
        assert only.executable_proposal() is None


def _docx(tmp_path: Path, name: str, paragraphs: list[str]) -> Path:
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    path = tmp_path / name
    document.save(path)
    return path


class TestElementIndexFromSpecs:
    def test_it_holds_every_element_the_review_was_shown(self, tmp_path):
        spec = extract_text_from_docx(
            _docx(tmp_path, "a.docx", ["SECTION 21 13 13", "Provide gate valve.", "Other."])
        )
        index = element_index_from_specs([spec])
        assert index == {"a.docx": {"p0": "SECTION 21 13 13", "p1": "Provide gate valve.", "p2": "Other."}}

    def test_it_validates_real_extracted_ids(self, tmp_path):
        spec = extract_text_from_docx(
            _docx(tmp_path, "a.docx", ["Intro.", "Provide gate valve.", "Also a gate valve here."])
        )
        occs = occurrences(
            finding(element_id="p1"),
            finding(element_id="p2"),
            finding(element_id="p0"),
            element_index=element_index_from_specs([spec]),
        )
        assert [(o.element_id, o.location) for o in occs] == [
            ("p1", LOCATION_VALIDATED),
            ("p2", LOCATION_VALIDATED),
            (None, LOCATION_UNRESOLVED),
        ]

    def test_block_delimiters_are_not_elements(self):
        spec = SimpleNamespace(
            filename="a.docx",
            paragraph_map=[
                SimpleNamespace(element_id="p0", text="Body."),
                SimpleNamespace(element_id="meta:fn", text="--- Footnotes ---"),
            ],
        )
        assert element_index_from_specs([spec]) == {"a.docx": {"p0": "Body."}}

    def test_a_name_two_specs_share_is_left_out(self):
        def spec(text):
            return SimpleNamespace(
                filename="a.docx", paragraph_map=[SimpleNamespace(element_id="p0", text=text)]
            )

        assert element_index_from_specs([spec("One."), spec("Two.")]) == {}

    def test_a_spec_without_a_map_contributes_nothing(self):
        assert element_index_from_specs([SimpleNamespace(filename="a.docx", paragraph_map=None)]) == {}
        assert element_index_from_specs(None) == {}


# ---------------------------------------------------------------------------
# Stable ids
# ---------------------------------------------------------------------------


class TestStableOccurrenceIds:
    def test_ids_do_not_depend_on_member_order(self):
        members = [finding(element_id="p8"), finding(element_id="p4"), finding(element_id=None)]
        seen = set()
        for order in itertools.permutations(members):
            seen.add(tuple(o.occurrence_id for o in occurrences(*order)))
        assert len(seen) == 1

    def test_ids_do_not_depend_on_finding_order_or_counters(self):
        first = merged(finding("a.docx", "p4"))
        second = merged(finding("b.docx", "p2", existingText="globe valve"))
        forward = {o.file_name: o.occurrence_id for g in group_findings([first, second]) for o in g.occurrences}
        backward = {o.file_name: o.occurrence_id for g in group_findings([second, first]) for o in g.occurrences}
        assert forward == backward
        assert all(value.startswith("oc-") and "grp" not in value for value in forward.values())

    def test_ids_do_not_depend_on_which_member_represents_the_group(self):
        """Equal severity and confidence: the representative is whichever
        arrived first, and the ids must not follow it."""
        a = [finding("a.docx", "p4"), finding("b.docx", "p2")]
        b = [finding("b.docx", "p2"), finding("a.docx", "p4")]
        ids_a = sorted(o.occurrence_id for o in occurrences(*a))
        ids_b = sorted(o.occurrence_id for o in occurrences(*b))
        assert ids_a == ids_b

    def test_module_identity_separates_identical_findings(self):
        fire = occurrences(finding(), module_id="datacenter_fire")
        electrical = occurrences(finding(), module_id="datacenter_electrical")
        assert fire[0].occurrence_id != electrical[0].occurrence_id
        assert fire[0].module_id == "datacenter_fire"

    def test_origin_prefixes_keep_one_place_apart(self):
        review, coordination = finding(), finding()
        _deduplicate_findings([review])
        pipeline.assign_cross_check_finding_ids([coordination])
        (r,) = group_findings([review])[0].occurrences
        (c,) = group_findings([coordination])[0].occurrences
        assert r.element_id == c.element_id == "p4"
        assert r.occurrence_id != c.occurrence_id

    def test_the_id_is_the_documented_digest(self):
        member = merged(finding())
        (only,) = group_findings([member], module_id="datacenter_fire")[0].occurrences
        target = ("element", "p4", pipeline._instruction_key(member.as_edit_proposal()))
        assert only.occurrence_id == compute_occurrence_id(
            "datacenter_fire", member.finding_id, "a.docx", target
        )

    def test_the_group_id_is_the_finding_id(self):
        member = merged(finding())
        (group,) = group_findings([member])
        assert group.group_id == member.finding_id

    def test_a_finding_never_given_an_id_is_not_given_one_here(self):
        """Identity comes only from the run's context (plan WP-06A), so the
        model mints no finding id; an id-less finding's occurrences rest on
        file, place, and instruction."""
        bare = finding()
        (group,) = group_findings([bare])
        assert bare.finding_id == "" and group.group_id == ""
        (first,) = group.occurrences
        (second,) = group_findings([finding()])[0].occurrences
        stamped = merged(finding())
        (third,) = group_findings([stamped])[0].occurrences
        assert first.occurrence_id == second.occurrence_id != third.occurrence_id


# ---------------------------------------------------------------------------
# Missing originals, and the representative vs. the original
# ---------------------------------------------------------------------------


class TestMissingOriginalStaysMissing:
    def _legacy(self):
        legacy = finding("a.docx", "p4", anchorText="near the valve schedule")
        legacy.affected_files = ["a.docx", "b.docx"]
        return legacy

    def test_a_listed_file_without_an_original_is_marked_missing(self):
        legacy = self._legacy()
        a, b = group_findings([legacy])[0].occurrences
        assert (a.file_name, a.location, a.original_finding) == ("a.docx", LOCATION_CLAIMED, legacy)
        assert (b.file_name, b.location, b.original_finding) == ("b.docx", LOCATION_MISSING_ORIGINAL, None)
        assert not b.has_original()
        assert "no original was recorded for b.docx" in b.location_note

    def test_it_borrows_no_element_and_no_anchor(self):
        _, b = group_findings([self._legacy()])[0].occurrences
        assert b.element_id is None
        proposal = b.executable_proposal()
        assert proposal.target_element_id is None
        assert proposal.anchor_text is None
        # The group's shared instruction text is all that is known.
        assert (proposal.existing_text, proposal.replacement_text) == ("gate valve", "ball valve")

    def test_an_addition_without_an_original_has_no_place(self):
        legacy = addition("a.docx", "p4")
        legacy.affected_files = ["a.docx", "b.docx"]
        _, b = group_findings([legacy])[0].occurrences
        proposal = b.executable_proposal()
        assert proposal.anchor_text is None and proposal.target_element_id is None

    def test_the_display_fallback_is_still_the_representative(self):
        legacy = self._legacy()
        _, b = group_findings([legacy])[0].occurrences
        assert b.executable_finding() is legacy

    def test_a_finding_that_names_no_file_is_one_placeholder(self):
        (only,) = group_findings([finding(file_name="")])[0].occurrences
        assert (only.file_name, only.location) == ("", LOCATION_MISSING_ORIGINAL)
        assert "names no file" in only.location_note


class TestRepresentativeAndOriginalStaySeparate:
    def test_display_fields_come_from_the_representative(self):
        low = finding("a.docx", "p4", severity="MEDIUM", confidence=0.4)
        high = finding("a.docx", "p8", severity="HIGH", confidence=0.9)
        group = merged(low, high)
        occs = group_findings([group])[0].occurrences
        assert all(o.finding is group for o in occs)
        assert {o.original_finding.severity for o in occs} == {"MEDIUM", "HIGH"}

    def test_each_place_executes_its_own_original(self):
        first = finding("a.docx", "p4", existingText="gate valve")
        second = finding("b.docx", "p9", existingText="Gate valve")
        occs = occurrences(first, second)
        by_file = {o.file_name: o.executable_proposal() for o in occs}
        assert by_file["a.docx"].existing_text == "gate valve"
        assert by_file["b.docx"].existing_text == "Gate valve"

    def test_a_demoted_original_has_no_instruction_and_borrows_none(self):
        group = merged(finding("a.docx", "p4"), finding("b.docx", "p9"))
        demoted = next(o for o in group.occurrence_originals if o.fileName == "b.docx")
        demoted.actionType = "REPORT_ONLY"
        demoted.edit_proposal = None
        by_file = {o.file_name: o for o in group_findings([group])[0].occurrences}
        assert by_file["a.docx"].executable_proposal() is not None
        assert by_file["b.docx"].executable_proposal() is None

    def test_a_report_only_group_has_occurrences_but_no_instruction(self):
        quiet = dict(actionType="REPORT_ONLY", existingText=None, replacementText=None)
        occs = occurrences(finding("a.docx", "p4", **quiet), finding("b.docx", "p2", **quiet))
        assert len(occs) == 2
        assert all(o.executable_proposal() is None for o in occs)


# ---------------------------------------------------------------------------
# A run's executable occurrences
# ---------------------------------------------------------------------------


class TestEditOccurrences:
    def test_report_only_findings_contribute_nothing(self):
        quiet = finding(actionType="REPORT_ONLY", existingText=None, replacementText=None)
        assert edit_occurrences([quiet]) == []

    def test_identical_coordination_findings_are_one_occurrence(self):
        twins = [finding(issue="Twin coordination finding."), finding(issue="Twin coordination finding.")]
        pipeline.assign_cross_check_finding_ids(twins)
        assert twins[0].finding_id == twins[1].finding_id
        (only,) = edit_occurrences(twins)
        assert len(only.also_reported_by) == 1

    def test_the_standing_twin_does_not_depend_on_order(self):
        one = finding(issue="Twin.", severity="MEDIUM")
        two = finding(issue="Twin.", severity="HIGH")
        pipeline.assign_cross_check_finding_ids([one, two])
        assert one.finding_id == two.finding_id
        (forward,) = edit_occurrences([one, two])
        (backward,) = edit_occurrences([two, one])
        assert forward.finding is two and backward.finding is two
        assert forward.also_reported_by == (one,) == backward.also_reported_by

    def test_twins_at_different_places_stay_apart(self):
        twins = [finding(element_id="p4"), finding(element_id="p8")]
        pipeline.assign_cross_check_finding_ids(twins)
        assert len(edit_occurrences(twins)) == 2

    def test_twins_whose_edits_differ_only_in_case_stay_apart(self):
        """Found in review (Codex, P1). Their key folds case, so both carry one
        ``cf-`` id; folding their instructions too listed one of them, the
        first in the input, so reversing two findings proposing "Ball valve"
        and "ball valve" changed the sidecar while it still listed one entry.
        Both are listed now, in either order, and neither reports the other's
        instruction."""

        def twins():
            pair = [finding(replacementText="Ball valve"), finding(replacementText="ball valve")]
            pipeline.assign_cross_check_finding_ids(pair)
            assert pair[0].finding_id == pair[1].finding_id
            return pair

        forward, backward = edit_occurrences(twins()), edit_occurrences(twins()[::-1])
        listed = [
            {(o.occurrence_id, o.executable_proposal().replacement_text) for o in occs}
            for occs in (forward, backward)
        ]
        assert listed[0] == listed[1]
        assert {text for _, text in listed[0]} == {"Ball valve", "ball valve"}
        assert not any(o.also_reported_by for o in forward + backward)

    def test_every_occurrence_id_is_listed_once(self):
        review = _deduplicate_findings(
            [finding("a.docx", "p4"), finding("a.docx", "p8"), finding("b.docx", "p2")]
        )
        coordination = [finding(issue="Coordination."), finding(issue="Coordination.")]
        pipeline.assign_cross_check_finding_ids(coordination)
        listed = [o.occurrence_id for o in edit_occurrences(review + coordination, module_id="m")]
        assert len(listed) == len(set(listed)) == 4


# ---------------------------------------------------------------------------
# The writer: schemas 6 and 7 (S12), and the sidecars it replaced, still read
# ---------------------------------------------------------------------------

_OCCURRENCE_GOLDEN = Path(__file__).parent / "goldens" / "occurrence_sidecar_payloads.json"


def _payload(review=(), cross=(), compliance=()):
    result = SimpleNamespace(
        review_result=ReviewResult(findings=list(review)),
        cross_check_result=ReviewResult(findings=list(cross)),
        compliance_result=ReviewResult(findings=list(compliance)),
        module_id="datacenter_fire",
        cycle_label="X",
    )
    data = edit_sidecar.build_edit_instructions(result)
    data.pop("generated_at")
    return data


def _scenarios() -> dict:
    """The scenarios the legacy golden was captured from, on master
    (``9919244``) before S11 changed the occurrence model, run through
    today's writer. Same inputs, so the two goldens compare scenario by
    scenario."""
    f = finding
    scenarios: dict = {}
    scenarios["singleton"] = _payload(_deduplicate_findings([f()]))
    scenarios["cross_file"] = _payload(
        _deduplicate_findings([f("a.docx", "p4"), f("b.docx", "p7"), f("c.docx", "p2")])
    )
    scenarios["same_file_p4_p8"] = _payload(_deduplicate_findings([f(element_id="p4"), f(element_id="p8")]))
    scenarios["same_file_p8_p4"] = _payload(_deduplicate_findings([f(element_id="p8"), f(element_id="p4")]))
    legacy = f()
    legacy.affected_files = ["a.docx", "b.docx"]
    scenarios["legacy_no_originals"] = _payload([legacy])
    scenarios["no_file"] = _payload([f(file_name="")])
    quiet = dict(actionType="REPORT_ONLY", existingText=None, replacementText=None)
    scenarios["report_only"] = _payload(
        _deduplicate_findings([f("a.docx", **quiet), f("b.docx", **quiet)])
    )
    twins = [f(), f()]
    pipeline.assign_cross_check_finding_ids(twins)
    scenarios["cross_check_twins"] = _payload(cross=twins)
    compliance = [f(file_name="c.docx")]
    pipeline.assign_compliance_finding_ids(compliance)
    scenarios["compliance"] = _payload(compliance=compliance)
    group = _deduplicate_findings([f("a.docx", "p4"), f("b.docx", "p9")])[0]
    group.occurrence_originals[1].edit_proposal = None
    group.occurrence_originals[1].actionType = "REPORT_ONLY"
    scenarios["demoted_original_borrows"] = _payload([group])
    scenarios["add_group"] = _payload(
        _deduplicate_findings(
            [
                f("a.docx", "p3", actionType="ADD", existingText=None, replacementText="New clause.", anchorText="Provide", insertPosition="after"),
                f("b.docx", None, actionType="ADD", existingText=None, replacementText="New clause.", anchorText="Install", insertPosition="before"),
            ]
        )
    )

    def child(module_id):
        return SimpleNamespace(
            review_result=ReviewResult(findings=_deduplicate_findings([f()])),
            cross_check_result=None,
            compliance_result=None,
            module_id=module_id,
        )

    program = SimpleNamespace(
        module_results={"datacenter_fire": child("datacenter_fire"), "datacenter_electrical": child("datacenter_electrical")},
        program_id="hyperscale",
        assignments=[],
        files_reviewed=["a.docx"],
        expected_files_reviewed=["a.docx"],
        routed_request_count=2,
        expected_routed_request_count=2,
    )
    data = edit_sidecar.build_edit_instructions(program)
    data.pop("generated_at")
    scenarios["program"] = data
    return scenarios


class TestTheWriter:
    """The sidecar writer emits schemas 6 and 7 (plan WP-06B, chunk S12).

    ``occurrence_sidecar_payloads.json`` pins every scenario. Regenerate it
    only when the writer changes on purpose, with
    ``SPEC_CRITIC_UPDATE_GOLDENS=1``, and review each scenario against
    ``legacy_sidecar_payloads.json``: what the writer emitted for the same
    inputs on master before S11.
    """

    def test_every_scenario_matches_the_golden(self):
        actual = json.loads(json.dumps(_scenarios(), sort_keys=True))
        if os.environ.get("SPEC_CRITIC_UPDATE_GOLDENS", "").strip().lower() in {"1", "true", "yes", "on"}:
            _OCCURRENCE_GOLDEN.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        expected = json.loads(_OCCURRENCE_GOLDEN.read_text(encoding="utf-8"))
        assert sorted(actual) == sorted(expected)
        for name in expected:
            assert actual[name] == expected[name], name

    def test_the_golden_holds_the_same_scenarios_as_the_legacy_one(self):
        legacy = json.loads(_GOLDEN.read_text(encoding="utf-8"))
        assert sorted(_scenarios()) == sorted(legacy)

    def test_the_schema_numbers_are_6_and_7(self):
        scenarios = _scenarios()
        assert scenarios["singleton"]["schema_version"] == 6
        assert scenarios["program"]["schema_version"] == 7

    def test_every_entry_is_one_occurrence(self):
        for name, payload in _scenarios().items():
            keys = [(e["module_id"], e["occurrence_id"]) for e in payload["edits"]]
            assert len(keys) == len(set(keys)), name
            for entry in payload["edits"]:
                assert entry["occurrence_id"].startswith("oc-"), name
                assert entry["location_basis"] in pipeline.LOCATION_BASES, name
                assert "has_per_file_original" not in entry, name

    def test_every_location_of_a_file_reaches_the_sidecar(self):
        """The first behavior S12 retires: one file's second place of a fix
        used to be dropped (the legacy golden has one entry here)."""
        for name in ("same_file_p4_p8", "same_file_p8_p4"):
            payload = _scenarios()[name]
            assert sorted(e["evidenceElementId"] for e in payload["edits"]) == ["p4", "p8"], name
        legacy = json.loads(_GOLDEN.read_text(encoding="utf-8"))
        assert [e["evidenceElementId"] for e in legacy["same_file_p4_p8"]["edits"]] == ["p4"]

    def test_no_place_borrows_another_places_locator(self):
        """The second behavior S12 retires: a file with no original, or with a
        demoted one, was lent the representative's locator — in
        ``demoted_original_borrows`` even another file's element."""
        scenarios = _scenarios()
        (b,) = [e for e in scenarios["demoted_original_borrows"]["edits"] if e["fileName"] == "b.docx"]
        assert b["edit_proposal"] is None and b["evidenceElementId"] == "p9"
        (b,) = [e for e in scenarios["legacy_no_originals"]["edits"] if e["fileName"] == "b.docx"]
        assert b["location_basis"] == LOCATION_MISSING_ORIGINAL
        assert b["evidenceElementId"] is None
        assert b["edit_proposal"]["target_element_id"] is None
        legacy = json.loads(_GOLDEN.read_text(encoding="utf-8"))
        (old,) = [e for e in legacy["demoted_original_borrows"]["edits"] if e["fileName"] == "b.docx"]
        assert old["edit_proposal"]["target_element_id"] == "p4"  # a.docx's element, lent to b.docx


class TestLegacySidecarsStillRead:
    """What Spec Critic wrote before S12 — the legacy golden, captured on
    master — reads exactly as schemas 4 and 5 always have."""

    @pytest.mark.parametrize("name", sorted(json.loads(_GOLDEN.read_text(encoding="utf-8"))))
    def test_every_legacy_payload_reads_as_before(self, tmp_path, name):
        from applier.sidecar import load_sidecar

        payload = json.loads(_GOLDEN.read_text(encoding="utf-8"))[name]
        assert payload["schema_version"] in (4, 5)
        path = tmp_path / "legacy.edits.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = load_sidecar(path)
        assert not loaded.is_occurrence_aware
        assert loaded.is_program is (payload["schema_version"] == 5)
        read = [*loaded.entries, *(entry for entry, _ in loaded.malformed)]
        assert len(read) == len(payload["edits"])
        by_key = {entry.key: entry for entry in loaded.entries}
        for raw in payload["edits"]:
            if (raw["finding_id"], raw["fileName"]) not in by_key:
                continue  # listed twice (a content twin), or malformed: accounted above
            entry = by_key[(raw["finding_id"], raw["fileName"])]
            assert entry.occurrence_id is None and entry.location_basis is None
            assert entry.has_per_file_original is raw["has_per_file_original"]
            assert entry.evidence_element_id == raw["evidenceElementId"]
            assert entry.target_element_id == raw["edit_proposal"]["target_element_id"]
