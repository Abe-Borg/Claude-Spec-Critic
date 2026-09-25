"""Tests for the machine-readable edit-instructions sidecar.

Spec Critic emits edit instructions but no longer applies them. After the
Word report is written, ``edit_sidecar.write_edit_instructions_sidecar``
drops a ``<report-stem>.edits.json`` file beside the report listing every
finding that carries an edit proposal, for a downstream applier to ingest.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from src.orchestration.pipeline import _deduplicate_findings
from src.output.edit_sidecar import (
    SIDECAR_SCHEMA_VERSION,
    build_edit_instructions,
    write_edit_instructions_sidecar,
)
from src.review.reviewer import EditProposal, Finding, ReviewResult
from src.verification.verifier import VerificationResult


@dataclass
class _StubPipelineResult:
    review_result: ReviewResult | None = None
    cross_check_result: ReviewResult | None = None
    cycle_label: str = "California 2025"


def _finding_with_edit(**kw) -> Finding:
    return Finding(
        severity=kw.get("severity", "HIGH"),
        fileName=kw.get("fileName", "Section_23_0000.docx"),
        section=kw.get("section", "2.1"),
        issue=kw.get("issue", "Stale code reference"),
        actionType="EDIT",
        existingText="2019 CBC",
        replacementText="2025 CBC",
        codeReference="CBC 2025",
        confidence=0.9,
        edit_proposal=EditProposal(
            action_type="EDIT",
            existing_text="2019 CBC",
            replacement_text="2025 CBC",
            edit_confidence=0.9,
        ),
    )


def _report_only_finding() -> Finding:
    return Finding(
        severity="MEDIUM",
        fileName="Section_23_0000.docx",
        section="3.0",
        issue="Coordination concern with structural.",
        actionType="REPORT_ONLY",
        existingText=None,
        replacementText=None,
        codeReference=None,
        confidence=0.7,
    )


def test_proposal_finding_emitted_in_payload():
    f = _finding_with_edit()
    f.verification = VerificationResult(verdict="CORRECTED", grounded=True, sources=["https://x"])
    result = _StubPipelineResult(review_result=ReviewResult(findings=[f]))
    payload = build_edit_instructions(result, report_path=Path("report.docx"))

    assert payload["schema_version"] == SIDECAR_SCHEMA_VERSION
    assert payload["report_file"] == "report.docx"
    assert payload["cycle_label"] == "California 2025"
    assert payload["edit_count"] == 1
    entry = payload["edits"][0]
    assert entry["fileName"] == "Section_23_0000.docx"
    assert entry["edit_proposal"]["existing_text"] == "2019 CBC"
    assert entry["edit_proposal"]["replacement_text"] == "2025 CBC"
    assert entry["verification_verdict"] == "CORRECTED"
    assert entry["report_status"] == "VERIFIED_CONTRADICTED"


def test_report_only_finding_omitted():
    result = _StubPipelineResult(
        review_result=ReviewResult(findings=[_report_only_finding()])
    )
    payload = build_edit_instructions(result)
    assert payload["edit_count"] == 0
    assert payload["edits"] == []


def test_cross_check_findings_included_in_payload():
    cc_finding = _finding_with_edit(section="4.0")
    cc = ReviewResult(findings=[cc_finding], cross_check_status="completed")
    result = _StubPipelineResult(
        review_result=ReviewResult(findings=[]), cross_check_result=cc
    )
    payload = build_edit_instructions(result)
    assert payload["edit_count"] == 1
    assert payload["edits"][0]["section"] == "4.0"


def test_write_sidecar_creates_file_next_to_report(tmp_path: Path):
    f = _finding_with_edit()
    result = _StubPipelineResult(review_result=ReviewResult(findings=[f]))
    report_path = tmp_path / "spec-critic-report-2026-05-27.docx"

    sidecar = write_edit_instructions_sidecar(result, report_path)

    assert sidecar == tmp_path / "spec-critic-report-2026-05-27.edits.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["edit_count"] == 1
    assert data["report_file"] == report_path.name


# ---------------------------------------------------------------------------
# Per-file fan-out (TRUST_AUDIT P0-1 / P0-2)
#
# When _deduplicate_findings collapses the same defect across N templated specs
# into one merged finding, the sidecar must still emit an actionable edit
# instruction for EVERY affected file — not just the representative — each with
# that file's own locator, while display/verification fields stay sourced from
# the (post-dedup, verified) representative.
# ---------------------------------------------------------------------------


def _edit_finding(
    *,
    file_name: str,
    issue: str = "Stale code reference",
    section: str = "2.1",
    severity: str = "HIGH",
    confidence: float = 0.9,
    action: str = "EDIT",
    existing: str | None = "2019 CBC",
    replacement: str | None = "2025 CBC",
    code_ref: str | None = "CBC 2025",
    anchor: str | None = None,
    insert_pos: str | None = None,
    evidence_id: str | None = None,
) -> Finding:
    """A finding built from legacy fields (no pre-set ``edit_proposal``), the
    shape ``_deduplicate_findings`` actually merges in production."""
    return Finding(
        severity=severity,
        fileName=file_name,
        section=section,
        issue=issue,
        actionType=action,
        existingText=existing,
        replacementText=replacement,
        codeReference=code_ref,
        confidence=confidence,
        anchorText=anchor,
        insertPosition=insert_pos,
        evidenceElementId=evidence_id,
    )


class TestMultiFileFanOut:
    def test_merged_multifile_finding_emits_one_entry_per_file(self):
        # P0-1: identical defect in two specs collapses to one merged finding
        # for display, but the sidecar emits an instruction for EACH file.
        merged = _deduplicate_findings(
            [_edit_finding(file_name="a.docx"), _edit_finding(file_name="b.docx")]
        )
        assert len(merged) == 1  # collapsed for the report
        payload = build_edit_instructions(
            _StubPipelineResult(review_result=ReviewResult(findings=merged))
        )

        assert payload["edit_count"] == 2
        assert {e["fileName"] for e in payload["edits"]} == {"a.docx", "b.docx"}
        for e in payload["edits"]:
            assert sorted(e["affected_files"]) == ["a.docx", "b.docx"]
            assert e["edit_proposal"]["existing_text"] == "2019 CBC"
            assert e["edit_proposal"]["replacement_text"] == "2025 CBC"
        # Entries from one finding share its content id; (finding_id, fileName)
        # is the unique per-entry key.
        ids = {e["finding_id"] for e in payload["edits"]}
        assert len(ids) == 1 and next(iter(ids))
        keys = [(e["finding_id"], e["fileName"]) for e in payload["edits"]]
        assert len(keys) == len(set(keys))

    def test_per_file_anchor_survives_merge_to_sidecar(self):
        # P0-2: anchorText is NOT in the dedup key, so files can carry
        # different anchors. The merge keeps only the representative's, but the
        # sidecar must emit each file's OWN anchor via executable_finding().
        merged = _deduplicate_findings(
            [
                _edit_finding(file_name="a.docx", anchor="after Part 1 General"),
                _edit_finding(file_name="b.docx", anchor="after Part 2 Products"),
            ]
        )
        assert len(merged) == 1
        payload = build_edit_instructions(
            _StubPipelineResult(review_result=ReviewResult(findings=merged))
        )

        anchors = {
            e["fileName"]: e["edit_proposal"]["anchor_text"] for e in payload["edits"]
        }
        assert anchors == {
            "a.docx": "after Part 1 General",
            "b.docx": "after Part 2 Products",
        }
        # Each file's own original located its entry; none borrowed (schema 6
        # states it as a location basis rather than has_per_file_original).
        assert all(e["location_basis"] != "missing_original" for e in payload["edits"])
        assert all("has_per_file_original" not in e for e in payload["edits"])

    def test_verification_fields_come_from_representative_for_every_file(self):
        # Verification runs AFTER dedup, so only the merged representative
        # carries a verdict; per-file originals have none. Every per-file entry
        # must still report the representative's verdict/status, not NOT_CHECKED.
        merged = _deduplicate_findings(
            [_edit_finding(file_name="a.docx"), _edit_finding(file_name="b.docx")]
        )
        merged[0].verification = VerificationResult(
            verdict="CORRECTED", grounded=True, sources=["https://example.gov/cbc"]
        )
        payload = build_edit_instructions(
            _StubPipelineResult(review_result=ReviewResult(findings=merged))
        )

        assert payload["edit_count"] == 2
        for e in payload["edits"]:
            assert e["verification_verdict"] == "CORRECTED"
            assert e["report_status"] == "VERIFIED_CONTRADICTED"

    def test_report_only_multifile_finding_emits_nothing(self):
        # A REPORT_ONLY finding that merged across files still produces zero
        # sidecar entries — consistent with the report, which renders the rep.
        merged = _deduplicate_findings(
            [
                _edit_finding(
                    file_name="a.docx",
                    action="REPORT_ONLY",
                    existing=None,
                    replacement=None,
                    code_ref=None,
                ),
                _edit_finding(
                    file_name="b.docx",
                    action="REPORT_ONLY",
                    existing=None,
                    replacement=None,
                    code_ref=None,
                ),
            ]
        )
        assert len(merged) == 1
        payload = build_edit_instructions(
            _StubPipelineResult(review_result=ReviewResult(findings=merged))
        )
        assert payload["edit_count"] == 0
        assert payload["edits"] == []

    def test_legacy_multifile_without_originals_borrows_nothing(self):
        # A finding with affected_files but no per-file originals (legacy /
        # resume payload): every file still gets an entry, and a file with no
        # original of its own is ``missing_original``. Schemas 4 and 5 lent it
        # the representative's locator under has_per_file_original=False;
        # schema 6 lends nothing (plan WP-06B): the entry carries the shared
        # edit text with no element and no anchor, so it is found by its text
        # or, for an addition, refused for want of a place.
        f = _edit_finding(file_name="a.docx", anchor="after Part 1")
        f.affected_files = ["a.docx", "b.docx"]
        f.finding_id = "rf-deadbeef0000"
        payload = build_edit_instructions(
            _StubPipelineResult(review_result=ReviewResult(findings=[f]))
        )

        assert payload["edit_count"] == 2
        by_file = {e["fileName"]: e for e in payload["edits"]}
        assert by_file["a.docx"]["location_basis"] == "unresolved"  # it names no element
        assert by_file["a.docx"]["edit_proposal"]["anchor_text"] == "after Part 1"
        assert by_file["b.docx"]["location_basis"] == "missing_original"
        assert by_file["b.docx"]["evidenceElementId"] is None
        assert by_file["b.docx"]["edit_proposal"]["anchor_text"] is None
        assert by_file["b.docx"]["edit_proposal"]["target_element_id"] is None
        assert by_file["a.docx"]["finding_id"] == by_file["b.docx"]["finding_id"]
        assert by_file["a.docx"]["occurrence_id"] != by_file["b.docx"]["occurrence_id"]


# ---------------------------------------------------------------------------
# B-33 — one named schema constant per record type
# ---------------------------------------------------------------------------


def test_sidecar_schema_constants_are_pinned():
    from src.output.edit_sidecar import (
        PROGRAM_SIDECAR_SCHEMA_VERSION,
        sidecar_schema_version_for,
    )

    # The emitted numbers: single-module payloads are v6, routed-program
    # payloads are v7 (one entry per occurrence, plan WP-06B; v4 / v5 before
    # chunk S12), and the two are independent.
    assert SIDECAR_SCHEMA_VERSION == 6
    assert PROGRAM_SIDECAR_SCHEMA_VERSION == 7
    assert sidecar_schema_version_for(_StubPipelineResult()) == 6

    @dataclass
    class _StubProgramResult:
        program_id: str = "hyperscale_datacenter"
        module_results: dict = None

    assert sidecar_schema_version_for(_StubProgramResult()) == 7


def test_program_payload_emits_program_schema_constant():
    from src.output.edit_sidecar import PROGRAM_SIDECAR_SCHEMA_VERSION

    f = _finding_with_edit()
    child = _StubPipelineResult(review_result=ReviewResult(findings=[f]))

    class _StubProgramResult:
        program_id = "hyperscale_datacenter"
        module_results = {"datacenter_fire": child}
        assignments: list = []
        files_reviewed = ["Section_23_0000.docx"]
        expected_files_reviewed = ["Section_23_0000.docx"]
        routed_request_count = 1
        expected_routed_request_count = 1
        project_profile = None
        module_errors: dict = {}

    payload = build_edit_instructions(_StubProgramResult(), report_path=Path("r.docx"))
    assert payload["schema_version"] == PROGRAM_SIDECAR_SCHEMA_VERSION == 7
    assert payload["edits"][0]["module_id"] == "datacenter_fire"
    # Additive: a stub without the attribute (and a clean result) emits an
    # empty list; a degraded result's warnings are carried verbatim.
    assert payload["integrity_warnings"] == []
    _StubProgramResult.integrity_warnings = ["request count clamped to 1"]
    degraded = build_edit_instructions(_StubProgramResult(), report_path=Path("r.docx"))
    assert degraded["integrity_warnings"] == ["request count clamped to 1"]
    # And the single-module child still emits its own constant.
    child_payload = build_edit_instructions(child, report_path=Path("r.docx"))
    assert child_payload["schema_version"] == SIDECAR_SCHEMA_VERSION == 6


# ---------------------------------------------------------------------------
# One entry per occurrence (schemas 6 and 7, plan WP-06B, chunk S12)
# ---------------------------------------------------------------------------


def _mapping(element_id: str, text: str):
    from src.input.extractor import ParagraphMapping

    return ParagraphMapping(
        body_index=int(element_id[1:]) if element_id[1:].isdigit() else 0,
        element_type="paragraph",
        text=text,
        table_index=None,
        row_index=None,
        cell_index=None,
        element_id=element_id,
    )


def _spec(name: str, paragraphs: dict[str, str]):
    from src.input.extractor import ExtractedSpec

    mappings = [_mapping(element_id, text) for element_id, text in paragraphs.items()]
    return ExtractedSpec(
        filename=name,
        content="\n\n".join(paragraphs.values()),
        word_count=0,
        paragraph_map=mappings,
    )


def _occurrence_finding(file_name="a.docx", element_id="p4", issue="Gate valves where ball valves are required.", **kw):
    return _edit_finding(
        file_name=file_name,
        issue=issue,
        existing="gate valve",
        replacement="ball valve",
        code_ref=None,
        evidence_id=element_id,
        **kw,
    )


@dataclass
class _RunResult:
    review_result: ReviewResult | None = None
    cross_check_result: ReviewResult | None = None
    compliance_result: ReviewResult | None = None
    extracted_specs: list | None = None
    module_id: str = "datacenter_fire"
    cycle_label: str = "X"


_A_TEXT = {
    "p1": "PART 2 PRODUCTS",
    "p4": "Provide a gate valve at each branch.",
    "p6": "Provide ball valves at each riser.",
    "p8": "Install a gate valve at the connection.",
}


class TestOccurrenceEntries:
    def test_each_place_is_validated_against_the_text_the_review_read(self):
        merged = _deduplicate_findings(
            [_occurrence_finding(element_id="p4"), _occurrence_finding(element_id="p8")]
        )
        payload = build_edit_instructions(
            _RunResult(review_result=ReviewResult(findings=merged), extracted_specs=[_spec("a.docx", _A_TEXT)])
        )
        assert [(e["evidenceElementId"], e["location_basis"], e["location_note"]) for e in payload["edits"]] == [
            ("p4", "validated", ""),
            ("p8", "validated", ""),
        ]
        for entry in payload["edits"]:
            assert entry["edit_proposal"]["target_element_id"] == entry["evidenceElementId"]
            assert entry["module_id"] == "datacenter_fire"

    def test_an_element_that_does_not_hold_the_text_is_not_emitted(self):
        """The review named p6, which does not contain "gate valve": the place
        is unresolved, and no element reaches the sidecar for it."""
        payload = build_edit_instructions(
            _RunResult(
                review_result=ReviewResult(findings=_deduplicate_findings([_occurrence_finding(element_id="p6")])),
                extracted_specs=[_spec("a.docx", _A_TEXT)],
            )
        )
        (entry,) = payload["edits"]
        assert entry["location_basis"] == "unresolved"
        assert entry["evidenceElementId"] is None
        assert entry["edit_proposal"]["target_element_id"] is None
        assert "p6" in entry["location_note"] and "does not contain" in entry["location_note"]

    def test_without_the_reviewed_text_an_element_is_claimed(self):
        payload = build_edit_instructions(
            _RunResult(review_result=ReviewResult(findings=_deduplicate_findings([_occurrence_finding()])))
        )
        (entry,) = payload["edits"]
        assert (entry["evidenceElementId"], entry["location_basis"]) == ("p4", "claimed")

    def test_the_key_is_module_and_occurrence_and_ids_do_not_follow_input_order(self):
        forward = [_occurrence_finding("a.docx", "p4"), _occurrence_finding("a.docx", "p8"), _occurrence_finding("b.docx", "p2")]
        backward = [_occurrence_finding("b.docx", "p2"), _occurrence_finding("a.docx", "p8"), _occurrence_finding("a.docx", "p4")]
        ids = []
        for findings in (forward, backward):
            payload = build_edit_instructions(_RunResult(review_result=ReviewResult(findings=_deduplicate_findings(findings))))
            keys = [(e["module_id"], e["occurrence_id"]) for e in payload["edits"]]
            assert len(keys) == len(set(keys)) == 3
            ids.append(sorted(occurrence for _, occurrence in keys))
        assert ids[0] == ids[1]

    def test_a_place_with_no_instruction_of_its_own_is_listed_without_one(self):
        """A place whose own finding proposes no usable edit borrows nothing:
        its entry has no proposal, and the applier refuses it with that
        reason instead of never hearing of the place."""
        import json as _json

        from applier.sidecar import load_sidecar

        (group,) = _deduplicate_findings([_occurrence_finding("a.docx", "p4"), _occurrence_finding("b.docx", "p9")])
        demoted = group.occurrence_originals[1]
        demoted.actionType = "REPORT_ONLY"
        payload = build_edit_instructions(_RunResult(review_result=ReviewResult(findings=[group])))
        by_file = {e["fileName"]: e for e in payload["edits"]}
        assert by_file["b.docx"]["edit_proposal"] is None
        assert by_file["a.docx"]["edit_proposal"]["existing_text"] == "gate valve"

        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as directory:
            path = _Path(directory) / "report.edits.json"
            path.write_text(_json.dumps(payload), encoding="utf-8")
            loaded = load_sidecar(path)
        assert [e.file_name for e in loaded.entries] == ["a.docx"]
        ((entry, why),) = loaded.malformed
        assert entry.file_name == "b.docx"
        assert "no edit instruction for this place" in why

    def test_a_program_names_every_entry_by_its_module_key(self):
        """A child result that carries no module id is still attributed, and
        one finding in two modules is two keys."""
        def child():
            return _StubPipelineResult(review_result=ReviewResult(findings=_deduplicate_findings([_occurrence_finding()])))

        class _Program:
            program_id = "hyperscale_datacenter"
            module_results = {"datacenter_fire": child(), "datacenter_electrical": child()}
            assignments: list = []
            files_reviewed = ["a.docx"]
            expected_files_reviewed = ["a.docx"]
            routed_request_count = 2
            expected_routed_request_count = 2

        payload = build_edit_instructions(_Program())
        assert payload["schema_version"] == 7
        keys = [(e["module_id"], e["occurrence_id"]) for e in payload["edits"]]
        assert [module for module, _ in keys] == ["datacenter_fire", "datacenter_electrical"]
        assert len({occurrence for _, occurrence in keys}) == 2
        assert len({e["finding_id"] for e in payload["edits"]}) == 1


def _verified(finding, verdict: str, *, grounded: bool = True, **flags):
    finding.verification = VerificationResult(
        verdict=verdict, grounded=grounded, sources=["https://example.gov/code"] if grounded else [], **flags
    )
    return finding


class TestContentTwinsCarryTheLeastTrustedStatus:
    """Two identical coordination findings share one ``cf-`` id and so one
    occurrence (listed once, by the reader's unique-key rule). Each carries
    its own verification; the entry takes the least trusted of them, whatever
    order they arrived in, so an applier never writes a change one of their
    verifications argued against."""

    def _twins(self, first_verdict, second_verdict, **second_flags):
        from src.orchestration.pipeline import assign_cross_check_finding_ids

        first = _verified(_occurrence_finding(issue="Coordination: valve schedules disagree."), first_verdict)
        second = _verified(_occurrence_finding(issue="Coordination: valve schedules disagree."), second_verdict, **second_flags)
        assign_cross_check_finding_ids([first, second])
        assert first.finding_id == second.finding_id
        return first, second

    def _entry(self, findings):
        payload = build_edit_instructions(
            _RunResult(review_result=ReviewResult(findings=[]), cross_check_result=ReviewResult(findings=findings))
        )
        (entry,) = payload["edits"]
        return entry

    def test_a_disputed_twin_holds_a_confirmed_one(self):
        first, second = self._twins("CONFIRMED", "DISPUTED")
        for order in ([first, second], [second, first]):
            entry = self._entry(order)
            assert (entry["report_status"], entry["verification_verdict"]) == ("DISPUTED", "DISPUTED")

    def test_an_unsettled_twin_outranks_a_supported_one(self):
        first, second = self._twins("CONFIRMED", "UNVERIFIED", grounded=False)
        for order in ([first, second], [second, first]):
            entry = self._entry(order)
            assert (entry["report_status"], entry["verification_verdict"]) == ("INSUFFICIENT_EVIDENCE", "UNVERIFIED")

    def test_agreeing_twins_keep_their_status(self):
        first, second = self._twins("CONFIRMED", "CONFIRMED")
        assert self._entry([first, second])["report_status"] == "VERIFIED_SUPPORTED"

    def test_the_trust_order_is_the_order_the_applier_admits_statuses_in(self):
        """Every policy that admits a status also admits every more trusted
        one, and the order names every status once."""
        from applier.policy import NEVER_AUTOMATIC, Policy, PolicyConfig
        from applier.models import EditEntry
        from src.output.report_status import STATUS_TRUST_ORDER, ReportStatus

        assert sorted(STATUS_TRUST_ORDER) == sorted(ReportStatus)

        def admitted(status):
            entry = EditEntry(
                finding_id="rf-x", file_name="a.docx", action_type="EDIT", existing_text="a",
                replacement_text="b", anchor_text=None, insert_position=None, target_element_id=None,
                evidence_element_id=None, edit_confidence=1.0, report_status=status.value,
                verification_verdict=None, severity="HIGH", section="", issue="", code_reference=None,
                has_per_file_original=True,
            )
            return {policy for policy in Policy if PolicyConfig(policy=policy).decide(entry).allowed}

        admissions = [admitted(status) for status in STATUS_TRUST_ORDER]
        for less, more in zip(admissions, admissions[1:]):
            assert less <= more
        never = [s for s in STATUS_TRUST_ORDER if s.value in NEVER_AUTOMATIC]
        assert STATUS_TRUST_ORDER[: len(never)] == tuple(never)
