"""Sidecar reading: both schemas, and the accounting rule that nothing vanishes.

The load path's job is not merely to parse. It is to guarantee that every
instruction the sidecar listed reaches the receipt as *something* — an
applied edit, a policy hold, or a named refusal. An entry that is silently
dropped at parse time reads downstream as a specification with no defect.
"""
from __future__ import annotations

import json

import pytest

from applier.models import EditEntry
from applier.sidecar import (
    PROGRAM_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    SidecarError,
    SidecarSchemaError,
    load_sidecar,
)


def _proposal(**overrides):
    base = {
        "action_type": "EDIT",
        "existing_text": "NFPA 13, 2019 edition",
        "replacement_text": "NFPA 13, 2025 edition",
        "anchor_text": None,
        "insert_position": None,
        "target_element_id": "p4",
        "edit_confidence": 0.82,
    }
    base.update(overrides)
    return base


def _entry(**overrides):
    base = {
        "finding_id": "rf-abc123",
        "fileName": "215000.docx",
        "affected_files": ["215000.docx"],
        "has_per_file_original": True,
        "section": "21 13 13",
        "severity": "HIGH",
        "issue": "Stale edition reference.",
        "codeReference": "NFPA 13",
        "evidenceElementId": "p4",
        "verification_verdict": "CORRECTED",
        "report_status": "VERIFIED_CONTRADICTED",
        "edit_proposal": _proposal(),
    }
    base.update(overrides)
    return base


def _write(tmp_path, payload, name="report.edits.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _sidecar(tmp_path, edits, *, version=4, **top):
    payload = {
        "schema_version": version,
        "generated_at": "2026-09-21T00:00:00Z",
        "report_file": "report.docx",
        "edit_count": len(edits),
        "edits": edits,
    }
    payload.update(top)
    return _write(tmp_path, payload)


class TestSchemaGate:
    def test_the_per_file_and_occurrence_aware_schemas_are_supported(self):
        # 4 / 5 mirror edit_sidecar.SIDECAR_SCHEMA_VERSION / PROGRAM_...; 6 / 7
        # are their occurrence-aware successors, read before the writer emits
        # them (plan WP-06B, chunk S11).
        assert SUPPORTED_SCHEMA_VERSIONS == {4, 5, 6, 7}

    def test_the_constants_match_the_writer(self):
        from src.output.edit_sidecar import (
            PROGRAM_SIDECAR_SCHEMA_VERSION,
            SIDECAR_SCHEMA_VERSION,
        )

        assert SIDECAR_SCHEMA_VERSION in SUPPORTED_SCHEMA_VERSIONS
        assert PROGRAM_SIDECAR_SCHEMA_VERSION in SUPPORTED_SCHEMA_VERSIONS
        assert PROGRAM_SCHEMA_VERSION == PROGRAM_SIDECAR_SCHEMA_VERSION

    @pytest.mark.parametrize("version", [3, 8, "4", "6", None])
    def test_an_unknown_schema_is_refused_not_guessed(self, tmp_path, version):
        path = _sidecar(tmp_path, [_entry()], version=version)
        with pytest.raises(SidecarSchemaError) as excinfo:
            load_sidecar(path)
        assert "schema_version" in str(excinfo.value)

    def test_unparseable_json_raises_rather_than_returning_empty(self, tmp_path):
        path = tmp_path / "broken.edits.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(SidecarError):
            load_sidecar(path)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(SidecarError):
            load_sidecar(tmp_path / "absent.json")

    def test_missing_edits_list_raises(self, tmp_path):
        path = _write(tmp_path, {"schema_version": 4})
        with pytest.raises(SidecarError):
            load_sidecar(path)


class TestFieldMapping:
    def test_every_field_survives_the_round_trip(self, tmp_path):
        loaded = load_sidecar(_sidecar(tmp_path, [_entry()]))
        (entry,) = loaded.entries
        assert isinstance(entry, EditEntry)
        assert entry.finding_id == "rf-abc123"
        assert entry.file_name == "215000.docx"
        assert entry.action_type == "EDIT"
        assert entry.existing_text == "NFPA 13, 2019 edition"
        assert entry.replacement_text == "NFPA 13, 2025 edition"
        assert entry.target_element_id == "p4"
        assert entry.evidence_element_id == "p4"
        assert entry.edit_confidence == pytest.approx(0.82)
        assert entry.report_status == "VERIFIED_CONTRADICTED"
        assert entry.verification_verdict == "CORRECTED"
        assert entry.code_reference == "NFPA 13"
        assert entry.has_per_file_original is True
        assert entry.affected_files == ("215000.docx",)
        assert entry.key == ("rf-abc123", "215000.docx")

    def test_program_schema_carries_module_id(self, tmp_path):
        loaded = load_sidecar(
            _sidecar(
                tmp_path,
                [_entry(module_id="datacenter_fire")],
                version=5,
                program_id="hyperscale_datacenter",
            )
        )
        assert loaded.is_program is True
        assert loaded.program_id == "hyperscale_datacenter"
        assert loaded.entries[0].module_id == "datacenter_fire"

    def test_single_module_schema_has_no_module_id(self, tmp_path):
        loaded = load_sidecar(_sidecar(tmp_path, [_entry()], cycle_label="California 2025"))
        assert loaded.is_program is False
        assert loaded.entries[0].module_id is None
        assert loaded.cycle_label == "California 2025"

    def test_integrity_warnings_are_carried_not_summarized(self, tmp_path):
        loaded = load_sidecar(
            _sidecar(
                tmp_path,
                [_entry()],
                version=5,
                integrity_warnings=["submitted_request_count clamped"],
            )
        )
        assert loaded.integrity_warnings == ["submitted_request_count clamped"]

    def test_a_clean_sidecar_has_no_integrity_warnings(self, tmp_path):
        assert load_sidecar(_sidecar(tmp_path, [_entry()])).integrity_warnings == []

    @pytest.mark.parametrize(
        "value, expected", [(None, 0.0), ("0.7", 0.7), (5, 1.0), (-2, 0.0), ("x", 0.0)]
    )
    def test_edit_confidence_is_clamped_not_trusted(self, tmp_path, value, expected):
        path = _sidecar(
            tmp_path, [_entry(edit_proposal=_proposal(edit_confidence=value))]
        )
        assert load_sidecar(path).entries[0].edit_confidence == pytest.approx(expected)

    def test_locator_text_follows_the_action(self, tmp_path):
        edit, add = load_sidecar(
            _sidecar(
                tmp_path,
                [
                    _entry(),
                    _entry(
                        finding_id="rf-add",
                        edit_proposal=_proposal(
                            action_type="ADD",
                            existing_text=None,
                            anchor_text="Provide hangers",
                            insert_position="after",
                        ),
                    ),
                ],
            )
        ).entries
        assert edit.locator_text == "NFPA 13, 2019 edition"
        assert add.locator_text == "Provide hangers"


class TestMalformedEntriesAreAccountedFor:
    @pytest.mark.parametrize(
        "proposal_overrides, fragment",
        [
            ({"action_type": "REPORT_ONLY"}, "unsupported action_type"),
            ({"action_type": "TRANSMUTE"}, "unsupported action_type"),
            ({"existing_text": None}, "without existing_text"),
            ({"replacement_text": None}, "EDIT without replacement_text"),
        ],
    )
    def test_an_unusable_entry_is_kept_and_explained(
        self, tmp_path, proposal_overrides, fragment
    ):
        path = _sidecar(tmp_path, [_entry(edit_proposal=_proposal(**proposal_overrides))])
        loaded = load_sidecar(path)
        assert loaded.entries == []
        (entry, why), = loaded.malformed
        assert fragment in why
        # It still carries enough identity for the receipt to name it.
        assert entry.finding_id == "rf-abc123"

    @pytest.mark.parametrize(
        "overrides, fragment",
        [
            ({"anchor_text": None, "target_element_id": None}, "anchor_text"),
            ({"replacement_text": None}, "ADD without replacement_text"),
            ({"insert_position": "sideways"}, "invalid insert_position"),
        ],
    )
    def test_add_shape_rules(self, tmp_path, overrides, fragment):
        proposal = _proposal(
            action_type="ADD",
            existing_text=None,
            anchor_text="Provide hangers",
            insert_position="after",
        )
        proposal.update(overrides)
        loaded = load_sidecar(_sidecar(tmp_path, [_entry(edit_proposal=proposal)]))
        assert loaded.entries == []
        assert fragment in loaded.malformed[0][1]

    def test_an_entry_naming_no_file_is_malformed(self, tmp_path):
        loaded = load_sidecar(_sidecar(tmp_path, [_entry(fileName="")]))
        assert loaded.entries == []
        assert "names no file" in loaded.malformed[0][1]

    def test_a_non_object_entry_is_still_counted(self, tmp_path):
        loaded = load_sidecar(_sidecar(tmp_path, ["not an object", _entry()]))
        assert len(loaded.entries) == 1
        assert len(loaded.malformed) == 1

    def test_nothing_is_lost_between_input_and_output(self, tmp_path):
        edits = [
            _entry(finding_id="rf-1"),
            _entry(finding_id="rf-2", edit_proposal=_proposal(action_type="REPORT_ONLY")),
            _entry(finding_id="rf-3", fileName=""),
            _entry(finding_id="rf-4"),
        ]
        loaded = load_sidecar(_sidecar(tmp_path, edits))
        assert len(loaded.entries) + len(loaded.malformed) == len(edits)

    def test_file_names_are_first_seen_ordered_and_unique(self, tmp_path):
        loaded = load_sidecar(
            _sidecar(
                tmp_path,
                [
                    _entry(fileName="b.docx"),
                    _entry(fileName="a.docx"),
                    _entry(fileName="b.docx"),
                ],
            )
        )
        assert loaded.file_names == ["b.docx", "a.docx"]
