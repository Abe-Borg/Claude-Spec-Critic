"""``ProgramPipelineResult.__post_init__`` degrades data inconsistencies.

The composite result is constructed once, at the end of collection — after
every review / verification / cross-check / compliance call has been billed.
Programming-error checks (program membership, module-id drift, error-map
shape) keep raising: they are collector invariants and the membership rule is
already enforced before any spend by ``ProgramSubmission``. Data re-hydrated
from per-child submission state (``submitted_files`` /
``submitted_request_count``) can be stale, so those checks degrade: WARNING
log, offending entries dropped / clamped, ``integrity_warnings`` recorded,
and the paid results still export.
"""
from __future__ import annotations

import logging

import pytest
from docx import Document

from src.orchestration import program_pipeline as pp
from src.output.edit_sidecar import build_edit_instructions
from src.output.html_report_exporter import render_html_report
from src.output.report_exporter import _program_run_diagnostics, export_report
from src.programs import HYPERSCALE_DATACENTER_PROGRAM
from tests.test_program_pipeline import _assignment, _result

PROGRAM_ID = HYPERSCALE_DATACENTER_PROGRAM.program_id
FIRE = "21 13 13 Fire Sprinklers.docx"
ARCH = "07 27 26 Air Barriers.docx"


def _two_module_result(**overrides):
    kwargs = dict(
        program_id=PROGRAM_ID,
        assignments=(
            _assignment(FIRE, ("datacenter_fire",)),
            _assignment(ARCH, ("datacenter_architecture",)),
        ),
        module_results={
            "datacenter_fire": _result("datacenter_fire", FIRE),
            "datacenter_architecture": _result("datacenter_architecture", ARCH),
        },
    )
    kwargs.update(overrides)
    return pp.ProgramPipelineResult(**kwargs)


class TestCleanResult:
    def test_carries_an_empty_integrity_list(self):
        result = _two_module_result()
        assert result.integrity_warnings == []
        assert result.files_reviewed == [FIRE, ARCH]
        assert result.routed_request_count == 2
        assert result.status == "completed"

    def test_explicit_consistent_values_record_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger=pp.__name__):
            result = _two_module_result(
                submitted_files=(FIRE, ARCH), submitted_request_count=2
            )
        assert result.integrity_warnings == []
        assert caplog.records == []


class TestUnknownSubmittedFileDegrades:
    def test_unknown_file_is_dropped_recorded_and_logged(self, caplog):
        with caplog.at_level(logging.WARNING, logger=pp.__name__):
            result = _two_module_result(
                submitted_files=(FIRE, "99 99 99 Moved.docx", ARCH),
            )

        # The paid results are intact and the known files are kept in order.
        assert set(result.module_results) == {
            "datacenter_fire", "datacenter_architecture"
        }
        assert result.files_reviewed == [FIRE, ARCH]
        assert len(result.integrity_warnings) == 1
        assert "99 99 99 Moved.docx" in result.integrity_warnings[0]
        assert "outside its assignments" in result.integrity_warnings[0]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "99 99 99 Moved.docx" in warnings[0].getMessage()
        assert PROGRAM_ID in warnings[0].getMessage()

    def test_report_and_sidecar_still_export(self, tmp_path):
        result = _two_module_result(
            submitted_files=(FIRE, "99 99 99 Moved.docx", ARCH),
        )
        report_path = export_report(result, tmp_path / "degraded.docx")
        text = "\n".join(p.text for p in Document(report_path).paragraphs)
        assert "Routed Specifications Submitted: 2 of 2" in text

        sidecar = build_edit_instructions(result, report_path=report_path)
        assert sidecar["submission_coverage"]["submitted_files"] == [FIRE, ARCH]
        assert sidecar["edit_count"] == 2


class TestRequestCountDegrades:
    def test_count_above_the_routed_range_is_clamped(self, caplog):
        with caplog.at_level(logging.WARNING, logger=pp.__name__):
            result = _two_module_result(submitted_request_count=7)
        assert result.routed_request_count == 2
        # A normalized figure is never "completed": the clamp is recorded and
        # the run presents as partial (amber terminal state in the GUI).
        assert result.status == "partial"
        assert any("clamped to 2" in w for w in result.integrity_warnings)
        assert any("clamped to 2" in r.getMessage() for r in caplog.records)

    def test_negative_count_is_clamped_to_zero(self):
        result = _two_module_result(submitted_request_count=-3)
        assert result.routed_request_count == 0
        assert result.status == "partial"
        assert any("clamped to 0" in w for w in result.integrity_warnings)


class TestIntegrityWarningsAreSurfaced:
    """A recorded degradation reaches every surface a reader looks at —
    ``status``, the program-level Run Diagnostics banner (Word + HTML), the
    HTML payload, and the program sidecar — so a normalized "2 of 2" can
    never pass as clean coverage."""

    ROW = "Result integrity warnings"

    @staticmethod
    def _degraded():
        return _two_module_result(
            submitted_files=(FIRE, "99 99 99 Moved.docx", ARCH),
        )

    def test_status_is_partial(self):
        assert self._degraded().status == "partial"
        assert _two_module_result().status == "completed"

    def test_aggregate_carries_the_warnings_only_when_present(self):
        degraded, _ = _program_run_diagnostics(self._degraded())
        assert len(degraded["integrity_warnings"]) == 1
        assert "99 99 99 Moved.docx" in degraded["integrity_warnings"][0]
        clean, _ = _program_run_diagnostics(_two_module_result())
        assert "integrity_warnings" not in clean

    def test_word_banner_renders_red_row_and_hint(self, tmp_path):
        from tests.test_diagnostic_banner import _banner_value, _banner_value_shading

        doc = Document(export_report(self._degraded(), tmp_path / "degraded.docx"))
        text = "\n".join(p.text for p in doc.paragraphs)
        assert _banner_value(doc, self.ROW) == "1"
        assert _banner_value_shading(doc, self.ROW) == "FFE5E5"
        assert "⚠ 1 result integrity warning:" in text
        assert "must be treated as unverified" in text
        assert "99 99 99 Moved.docx" in text
        # The normalized figure still renders — now next to its warning.
        assert "Routed Specifications Submitted: 2 of 2" in text

    def test_clean_word_report_has_no_row_or_hint(self, tmp_path):
        from tests.test_diagnostic_banner import _banner_value

        doc = Document(export_report(_two_module_result(), tmp_path / "clean.docx"))
        text = "\n".join(p.text for p in doc.paragraphs)
        assert _banner_value(doc, self.ROW) is None
        assert "result integrity warning" not in text

    def test_html_report_mirrors_the_row_hint_and_payload(self):
        from tests.test_html_report_exporter import _data_payload, _plaintext

        html = render_html_report(self._degraded())
        assert (
            f"<tr><th>{self.ROW}</th><td class=\"sc-flag\">1</td></tr>" in html
        )
        text = _plaintext(html)
        assert "⚠ 1 result integrity warning:" in text
        assert "99 99 99 Moved.docx" in text
        payload = _data_payload(html)
        assert len(payload["run_diagnostics"]["integrity_warnings"]) == 1
        clean_html = render_html_report(_two_module_result())
        assert self.ROW not in clean_html
        assert "integrity_warnings" not in _data_payload(clean_html)["run_diagnostics"]

    def test_sidecar_carries_the_warnings(self, tmp_path):
        degraded = build_edit_instructions(
            self._degraded(), report_path=tmp_path / "degraded.docx"
        )
        assert len(degraded["integrity_warnings"]) == 1
        assert "99 99 99 Moved.docx" in degraded["integrity_warnings"][0]
        # The normalized coverage is still exported, flagged by the list.
        assert degraded["submission_coverage"]["submitted_files"] == [FIRE, ARCH]
        clean = build_edit_instructions(
            _two_module_result(), report_path=tmp_path / "clean.docx"
        )
        assert clean["integrity_warnings"] == []


class TestProgrammingErrorsStillRaise:
    """Collector invariants — reaching these means a bug, not stale data."""

    def test_module_id_drift_raises(self):
        with pytest.raises(ValueError, match="contains"):
            _two_module_result(
                module_results={
                    "datacenter_fire": _result("datacenter_architecture", FIRE),
                }
            )

    def test_error_for_module_outside_catalog_raises(self):
        with pytest.raises(ValueError, match="outside its catalog"):
            _two_module_result(module_errors={"not_a_module": "boom"})

    def test_error_for_unassigned_module_raises(self):
        with pytest.raises(ValueError, match="unassigned"):
            _two_module_result(module_errors={"datacenter_electrical": "boom"})

    def test_result_and_error_for_the_same_module_raises(self):
        with pytest.raises(ValueError, match="both a completed result"):
            _two_module_result(module_errors={"datacenter_fire": "boom"})

    def test_cross_program_assignment_raises(self):
        with pytest.raises(ValueError, match="belongs to"):
            pp.ProgramPipelineResult(
                program_id="california_k12",
                assignments=(_assignment(FIRE, ("datacenter_fire",)),),
                module_results={},
            )
