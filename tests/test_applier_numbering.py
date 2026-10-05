"""The applier's side of plan WP-03: an automatic number is display text (chunk S14).

Since S14 the review reads an automatically numbered paragraph as Word shows
it — "A. Provide the specified piping system." — although no run holds "A.".
A finding can therefore quote a number the document does not contain. These
tests hold the boundary the writer draws:

* **A number is never edited.** A DELETE that includes one, or an EDIT that
  changes or removes one, is refused with a reason naming it, and nothing is
  written.
* **Body text after a number still locates**, whether the finding quotes the
  number as unchanged context or not; the offsets are translated only when
  the match is exact and the replacement keeps the quoted characters.
* **An addition beside a numbered paragraph is numbered by Word**, so a
  leading copy of the number Word will give it is dropped, and a different
  number of the same shape is refused.
* **End to end**: extraction → review prompt → finding → anchor validation →
  sidecar → applier → receipt, on a real numbered document.

Hermetic: documents are built in memory and saved only under ``tmp_path``.
"""
from __future__ import annotations

import pytest
from docx import Document
from docx.oxml.ns import qn

from applier import docx_edit as docx_edit_module
from applier.conflicts import settle
from applier.docx_edit import DocumentEditor, EditError
from applier.locator import build_candidates, classify_element_id, locate
from applier.models import ElementKind, LocationStatus, OutcomeStatus
from applier.run import RunSettings
from src.input.extractor import _accept_all_paragraph_text, extract_text_from_docx
from src.input.numbering import resolve_numbering
from src.review.review_request_builder import ReviewRequestSpec, build_user_message
from src.review.reviewer import Finding, validate_finding_anchors
from tests.fixtures import spec_docx as fx
from tests.test_applier_wrapped_content import at, entry, reject_all
from tests.test_occurrence_end_to_end import _result, _run_chain

lvl = fx.numbering_level

_PROVIDE = "Provide the specified piping system."


def _numbered_document():
    """The WP-01 fixture: PART / article / lettered paragraph, numbered by Word.
    ``p2`` shows "A. Provide the specified piping system."."""
    return fx.build_auto_numbered_three_part().document


def _body(document) -> list:
    return document.element.body.findall(qn("w:p"))


def _shown(document) -> list[str]:
    """Every paragraph as Word shows it after accepting all changes."""
    numbering = resolve_numbering(document)
    out = []
    for p_el in _body(document):
        label = numbering.label(p_el)
        out.append((label.prefix if label else "") + _accept_all_paragraph_text(p_el))
    return out


def _apply(document, the_entry, element_id: str = "p2") -> str:
    editor = DocumentEditor(document)
    location = at(element_id, classify_element_id(element_id))
    return editor.apply_planned(editor.plan(the_entry, editor.resolve(location)))


def _refused(document, the_entry, element_id: str = "p2") -> str:
    before = document.element.body.xml
    with pytest.raises(EditError) as excinfo:
        _apply(document, the_entry, element_id)
    assert document.element.body.xml == before, "a refused edit must not change the document"
    return str(excinfo.value)


# ---------------------------------------------------------------------------
# EDIT and DELETE
# ---------------------------------------------------------------------------


class TestBodyTextAfterANumber:
    def test_an_edit_that_does_not_quote_the_number(self):
        document = _numbered_document()
        _apply(document, entry(existing_text="Provide the specified", replacement_text="Provide the scheduled"))
        assert _shown(document)[2] == "A. Provide the scheduled piping system."
        assert reject_all(_body(document)[2]) == _PROVIDE

    def test_an_edit_that_quotes_the_number_as_context(self):
        document = _numbered_document()
        note = _apply(document, entry(
            existing_text=f"A. {_PROVIDE}",
            replacement_text="A. Provide the specified copper piping system.",
        ))
        assert "'Provide the specified piping system.'" in note
        assert _shown(document)[2] == "A. Provide the specified copper piping system."
        assert reject_all(_body(document)[2]) == _PROVIDE  # nothing but the body text moved

    def test_a_heading_edit_that_keeps_its_number(self):
        document = _numbered_document()
        _apply(document, entry(existing_text="1.02 SUBMITTALS", replacement_text="1.02 SUBMITTALS AND SAMPLES"), "p3")
        assert _shown(document)[3] == "1.02 SUBMITTALS AND SAMPLES"

    def test_part_of_the_number_quoted_as_context(self):
        document = _numbered_document()
        _apply(document, entry(existing_text=". Provide the", replacement_text=". Furnish the"))
        assert _shown(document)[2] == "A. Furnish the specified piping system."

    def test_a_dry_plan_equals_the_real_one(self):
        document = _numbered_document()
        editor = DocumentEditor(document)
        plan = editor.plan(
            entry(existing_text=f"A. {_PROVIDE}", replacement_text="A. Provide pipe."), editor.resolve(at("p2"))
        )
        assert (plan.span, plan.written_text) == ((0, len(_PROVIDE)), "Provide pipe.")


class TestANumberIsNeverEdited:
    @pytest.mark.parametrize(
        "element_id, existing, replacement",
        [
            ("p1", "1.01 SUMMARY", "1.02 SUMMARY"),  # changes the number
            ("p2", "A. Provide the specified", "Provide the specified"),  # removes it
            ("p2", "A.", "B."),  # only the number
            ("p1", "1.01", "1.01 "),  # ends inside it
            ("p1", "1.01 ", "1.01 NEW "),  # the number and its space only: no text of the document
            ("p2", "A.  Provide the specified", "A.  Provide the scheduled"),  # not exact: unproven
        ],
    )
    def test_an_edit_touching_the_number(self, element_id, existing, replacement):
        reason = _refused(
            _numbered_document(),
            entry(existing_text=existing, replacement_text=replacement),
            element_id,
        )
        assert "automatic number" in reason and "Change the numbering in Word" in reason

    def test_a_deletion_including_the_number(self):
        reason = _refused(_numbered_document(), entry(action_type="DELETE", existing_text=f"A. {_PROVIDE}"))
        assert "automatic number 'A.'" in reason

    def test_a_deletion_of_body_text_only(self):
        document = _numbered_document()
        _apply(document, entry(action_type="DELETE", existing_text=" piping"))
        assert _shown(document)[2] == "A. Provide the specified system."

    def test_the_same_text_as_a_number_and_typed_is_ambiguous(self):
        builder = fx.SpecDocBuilder()
        num = builder.define_num(builder.define_list(lvl(0, "upperLetter")))
        cell = lambda xml: f"<w:tc>{xml}</w:tc>"
        builder.add_xml(
            "<w:tbl><w:tblPr/><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid><w:tr>"
            + cell(fx.paragraph("Pipe", properties=fx.numbered_properties(num, 0)))
            + cell(fx.paragraph("A. Pipe"))
            + "</w:tr></w:tbl>"
        )
        reason = _refused(
            builder.document,
            entry(existing_text="A. Pipe", replacement_text="A. Tube"),
            "t0r0",
        )
        assert "occurs 2 times" in reason

    def test_a_numbered_cell_paragraph_edits_its_own_text(self):
        builder = fx.SpecDocBuilder()
        num = builder.define_num(builder.define_list(lvl(0, "upperLetter")))
        builder.add_xml(
            "<w:tbl><w:tblPr/><w:tblGrid><w:gridCol/></w:tblGrid><w:tr><w:tc>"
            + fx.paragraph("Copper pipe", properties=fx.numbered_properties(num, 0))
            + fx.paragraph("Steel pipe", properties=fx.numbered_properties(num, 0))
            + "</w:tc></w:tr></w:tbl>"
        )
        document = builder.document
        _apply(document, entry(existing_text="B. Steel pipe", replacement_text="B. Ductile iron pipe"), "t0r0")
        cell_paragraphs = list(document.element.body.iter(qn("w:p")))
        assert _accept_all_paragraph_text(cell_paragraphs[1]) == "Ductile iron pipe"

    def test_unreadable_numbering_refuses_rather_than_guesses(self, monkeypatch):
        def broken(document):
            raise RuntimeError("corrupt numbering part")

        monkeypatch.setattr(docx_edit_module, "resolve_numbering", broken)
        document = _numbered_document()
        reason = _refused(document, entry(existing_text=f"A. {_PROVIDE}", replacement_text="A. Provide pipe."))
        assert "not found" in reason
        _apply(document, entry(existing_text=_PROVIDE, replacement_text="Provide pipe."))
        assert _accept_all_paragraph_text(_body(document)[2]) == "Provide pipe."


# ---------------------------------------------------------------------------
# ADD
# ---------------------------------------------------------------------------


def _addition(text: str, *, anchor: str | None = _PROVIDE, position: str = "after"):
    return entry(action_type="ADD", replacement_text=text, anchor_text=anchor, insert_position=position)


class TestAdditionsBesideANumberedParagraph:
    def test_a_copy_of_the_next_number_is_dropped(self):
        document = _numbered_document()
        note = _apply(document, _addition("B. Provide pipe hangers."))
        assert "'Provide pipe hangers.'" in note
        assert _shown(document)[2:5] == [f"A. {_PROVIDE}", "B. Provide pipe hangers.", "1.02 SUBMITTALS"]

    def test_before_the_anchor_it_takes_the_anchors_number(self):
        document = _numbered_document()
        _apply(document, _addition("A. Provide pipe hangers.", position="before"))
        assert _shown(document)[2:4] == ["A. Provide pipe hangers.", f"B. {_PROVIDE}"]

    def test_text_without_a_number_is_written_as_it_is(self):
        document = _numbered_document()
        _apply(document, _addition("Provide pipe hangers."))
        assert _shown(document)[3] == "B. Provide pipe hangers."

    def test_a_number_word_will_not_give_it_is_refused(self):
        reason = _refused(_numbered_document(), _addition("C. Provide pipe hangers."))
        assert "numbered 'B.' automatically" in reason and "'C.'" in reason

    def test_an_article_number_under_an_article(self):
        reason = _refused(_numbered_document(), _addition("1.03 WARRANTY", anchor="SUMMARY"), "p1")
        assert "numbered '1.02' automatically" in reason and "'1.03'" in reason

    def test_a_word_that_is_not_a_number_of_this_level_is_kept(self):
        """Only a label of the level's own shape counts as a number: a letter
        label repeats one letter ("A.", "AA."), so "NOTE." is text."""
        document = _numbered_document()
        _apply(document, _addition("NOTE. Coordinate sleeves with the structural drawings."))
        assert _shown(document)[3] == "B. NOTE. Coordinate sleeves with the structural drawings."

    @pytest.mark.parametrize(
        "text, why",
        [
            ("2.New requirement.", "cannot be told"),
            ("2.5 inches of cover.", "cannot be told"),
            ("3.New requirement.", "begins with the number '3.'"),
        ],
    )
    def test_with_nothing_after_the_number_a_leading_number_is_refused(self, text, why):
        """A level whose suffix is ``nothing`` shows its number against the
        text ("1.Old"). A leading number there cannot be told apart from the
        text itself ("2.5 inches"), so it is neither kept — Word would show
        "2.2.New" — nor dropped, which could rewrite the text; the addition is
        refused (found by review: it was written as it was)."""
        builder = fx.SpecDocBuilder()
        num = builder.define_num(builder.define_list(lvl(0, suffix="nothing")))
        builder.add_numbered("Old requirement.", num, 0)
        reason = _refused(builder.document, _addition(text, anchor="Old requirement."), "p0")
        assert "numbered '2.' automatically" in reason and why in reason

    @pytest.mark.parametrize("text", ["New requirement.", "2. New requirement."])
    def test_with_nothing_after_the_number_other_text_is_added(self, text):
        builder = fx.SpecDocBuilder()
        num = builder.define_num(builder.define_list(lvl(0, suffix="nothing")))
        builder.add_numbered("Old requirement.", num, 0)
        document = builder.document
        _apply(document, _addition(text, anchor="Old requirement."), "p0")
        assert _shown(document) == ["1.Old requirement.", "2.New requirement."]

    def test_only_the_number_is_refused(self):
        reason = _refused(_numbered_document(), _addition("B."))
        assert "only the number 'B.'" in reason

    def test_an_anchor_may_quote_the_number(self):
        document = _numbered_document()
        _apply(document, _addition("Provide pipe hangers.", anchor="1.01"), "p1")
        assert _shown(document)[2] == "1.02 Provide pipe hangers."

    def test_beside_an_unnumbered_paragraph_nothing_changes(self):
        document = fx.build_clean_three_part().document
        _apply(document, _addition("B. Provide pipe hangers.", anchor="A. Provide the specified"))
        assert _accept_all_paragraph_text(_body(document)[3]) == "B. Provide pipe hangers."


# ---------------------------------------------------------------------------
# The locator and the settlement
# ---------------------------------------------------------------------------


class TestLocatorAndSettlement:
    def test_the_displayed_text_locates_by_id_and_by_text(self, tmp_path):
        spec = extract_text_from_docx(fx.save_docx(fx.build_auto_numbered_three_part(), tmp_path, "a.docx"))
        candidates = build_candidates(spec)
        quoted = entry(existing_text=f"A. {_PROVIDE}", replacement_text="x", evidence_element_id="p2")
        assert locate(quoted, candidates).status is LocationStatus.RESOLVED_BY_ID
        unnamed = entry(existing_text=f"A. {_PROVIDE}", replacement_text="x")
        location = locate(unnamed, candidates)
        assert (location.status, location.element_id) == (LocationStatus.RESOLVED_BY_UNIQUE_TEXT, "p2")
        assert location.kind is ElementKind.BODY_PARAGRAPH

    def test_quoting_the_number_or_not_is_one_change(self):
        document = _numbered_document()
        editor = DocumentEditor(document)
        paragraphs = editor.resolve(at("p2"))
        plans = [
            editor.plan(entry(existing_text=f"A. {_PROVIDE}", replacement_text="A. Provide pipe."), paragraphs),
            editor.plan(entry(existing_text=_PROVIDE, replacement_text="Provide pipe."), paragraphs),
        ]
        settlement = settle(plans)
        assert settlement.duplicates == {1: 0} and settlement.conflicts == {}

    def test_an_addition_with_and_without_the_number_is_one_change(self):
        document = _numbered_document()
        editor = DocumentEditor(document)
        paragraphs = editor.resolve(at("p2"))
        plans = [editor.plan(_addition("B. Provide hangers."), paragraphs),
                 editor.plan(_addition("Provide hangers."), paragraphs)]
        assert settle(plans).duplicates == {1: 0}


# ---------------------------------------------------------------------------
# The boundary: extraction → prompt → finding → sidecar → applier → receipt
# ---------------------------------------------------------------------------


def _finding(element_id: str, action: str, **fields) -> Finding:
    base = dict(
        severity="HIGH",
        fileName="230500.docx",
        section="",
        issue="Numbered-spec edit.",
        actionType=action,
        existingText=None,
        replacementText=None,
        codeReference=None,
        confidence=0.9,
        evidenceElementId=element_id,
    )
    base.update(fields)
    return Finding(**base)


class TestExtractionToApplier:
    def test_every_step_sees_the_numbers_and_only_body_text_is_edited(self, tmp_path):
        source = fx.save_docx(fx.build_auto_numbered_three_part(), tmp_path / "in", "230500.docx")
        spec = extract_text_from_docx(source)

        # The review reads the numbers, with the ids a finding cites.
        prompt = build_user_message(ReviewRequestSpec(
            spec_content=spec.content, filename=spec.filename,
            model="claude-opus-5", paragraph_map=spec.paragraph_map,
        ))
        for quoted in (
            f'<para id="p2">A. {_PROVIDE}</para>',
            '<heading id="p6">2.01 MATERIALS</heading>',
            '<para id="p4">A. Submit product data before fabrication.</para>',
        ):
            assert quoted in prompt

        # Findings as a review model writes them: quoting what it was shown.
        findings = [
            _finding("p2", "EDIT", existingText=f"A. {_PROVIDE}",
                     replacementText="A. Provide the specified copper piping system.",
                     section="1.01 SUMMARY", issue="Name the piping material."),
            _finding("p6", "EDIT", existingText="2.01 MATERIALS", replacementText="2.02 MATERIALS",
                     section="2.01 MATERIALS", issue="Renumber the article."),
            _finding("p4", "ADD", anchorText="A. Submit product data before fabrication.",
                     replacementText="B. Submit shop drawings before fabrication.",
                     insertPosition="after", section="1.02 SUBMITTALS", issue="Add shop drawings."),
            _finding("p10", "DELETE",
                     existingText="A. Install in accordance with the approved product instructions.",
                     section="3.01 INSTALLATION", issue="Remove the redundant requirement."),
        ]
        # The quotes are in the reviewed text, so none is demoted.
        assert validate_finding_anchors(findings, {spec.filename: spec.content}) == 0

        chain = _run_chain(tmp_path, _result([spec], findings), [source], RunSettings())
        outcomes = _outcomes_by_action(chain)
        assert outcomes["EDIT p2"][0] == OutcomeStatus.APPLIED.value
        assert outcomes["ADD p4"][0] == OutcomeStatus.APPLIED.value
        assert outcomes["EDIT p6"][0] == OutcomeStatus.UNLOCATED.value
        assert "automatic number '2.01'" in outcomes["EDIT p6"][1]
        assert outcomes["DELETE p10"][0] == OutcomeStatus.UNLOCATED.value
        assert "automatic number 'A.'" in outcomes["DELETE p10"][1]

        (result,) = chain.results
        edited = Document(result.output_path)
        assert _shown(edited) == [
            "PART 1 GENERAL", "1.01 SUMMARY",
            "A. Provide the specified copper piping system.",
            "1.02 SUBMITTALS", "A. Submit product data before fabrication.",
            "B. Submit shop drawings before fabrication.",
            "PART 2 PRODUCTS", "2.01 MATERIALS",
            "A. Provide materials meeting the scheduled requirements.",
            "PART 3 EXECUTION", "3.01 INSTALLATION",
            "A. Install in accordance with the approved product instructions.",
        ]
        # The source is untouched, and the edited copy reads back through the
        # extractor with every number where Word shows it.
        assert extract_text_from_docx(source).content == spec.content
        reread = extract_text_from_docx(fx.save_docx(edited, tmp_path / "reread", "230500.docx"))
        assert reread.extraction_warnings == []
        assert "B. Submit shop drawings before fabrication." in reread.content


def _outcomes_by_action(chain) -> dict[str, tuple[str, str]]:
    """``{"<action> <element>": (outcome, reason)}`` over the receipt."""
    sidecar_entries = {entry["occurrence_id"]: entry for entry in chain.sidecar["edits"]}
    found = {}
    for file_entry in chain.receipt["files"]:
        for outcome in file_entry["outcomes"]:
            source = sidecar_entries[outcome["occurrence_id"]]
            key = f"{source['edit_proposal']['action_type']} {source['evidenceElementId']}"
            found[key] = (outcome["outcome"], outcome.get("reason") or "")
    return found
