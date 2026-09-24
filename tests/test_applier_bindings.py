"""The applier binds each document to exactly one supplied file (plan WP-07, S02).

A sidecar names its documents by file name. The applier used to map each name
to the first supplied path with that name, so given two projects' ``spec.docx``
it edited whichever came first — reverse the inputs and it edited the other
project's copy. These tests pin the replacement contract:

* a name that matches two *different* supplied files is ``FILE_AMBIGUOUS``, in
  either input order; the same file supplied twice, or by two spellings of one
  path, is one input;
* every binding and destination is decided before the first write, and a
  destination that would overwrite *any* supplied specification — or another
  document's edited copy — is ``DESTINATION_CONFLICT``;
* a held document holds all of its instructions, each with a specific reason,
  in a balanced receipt; the other documents stay actionable; the CLI exits 3
  with or without ``--strict``;
* ``--assist`` is never consulted for a held document, and a dry run makes the
  same decisions as a real run.

Everything runs on generated documents in ``tmp_path``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from applier import run as run_module
from applier.assist import AssistConfig
from applier.cli import EXIT_INPUT_HELD, EXIT_OK, main
from applier.models import FileResult, OutcomeStatus
from applier.receipt import build_receipt, render_summary
from applier.run import RunSettings, apply_sidecar
from applier.sidecar import load_sidecar
from src.input.extractor import extract_text_from_docx
from tests.fixtures import spec_docx as fx

_EDIT_P4 = {
    "action_type": "EDIT",
    "existing_text": "before fabrication",
    "replacement_text": "before ordering materials",
    "anchor_text": None,
    "insert_position": None,
    "target_element_id": "p4",
    "edit_confidence": 0.9,
}


def _spec(directory: Path, name: str = "spec.docx") -> Path:
    return fx.save_docx(fx.build_clean_three_part(), directory, name)


def _edit(finding_id: str, file_name: str = "spec.docx", **proposal) -> dict:
    fields = dict(_EDIT_P4)
    fields.update(proposal)
    return {
        "finding_id": finding_id,
        "fileName": file_name,
        "affected_files": [file_name],
        "has_per_file_original": True,
        "section": "1.02 SUBMITTALS",
        "severity": "HIGH",
        "issue": "Submittal timing.",
        "codeReference": None,
        "evidenceElementId": fields["target_element_id"],
        "verification_verdict": "CONFIRMED",
        "report_status": "VERIFIED_SUPPORTED",
        "edit_proposal": fields,
    }


# An EDIT whose text sits in two paragraphs of the clean fixture (p2, p7)
# with no element id and a section that picks neither: AMBIGUOUS *inside* a
# document, which is the one case ``--assist`` exists for.
_IN_DOCUMENT_AMBIGUITY = dict(existing_text="Provide", replacement_text="Furnish", target_element_id=None)


def _write_sidecar(directory: Path, edits: list[dict]) -> Path:
    payload = {
        "schema_version": 4,
        "generated_at": "2026-09-24T00:00:00Z",
        "report_file": "report.docx",
        "edit_count": len(edits),
        "edits": edits,
    }
    path = directory / "report.edits.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run(tmp_path: Path, edits: list[dict], inputs: list[Path], settings=None, **kwargs):
    sidecar = load_sidecar(_write_sidecar(tmp_path, edits))
    return sidecar, apply_sidecar(sidecar, inputs, settings or RunSettings(), **kwargs)


def _by_id(results) -> dict:
    return {o.entry.finding_id: o for r in results for o in r.outcomes}


def _copies(root: Path) -> list[str]:
    """Every edited copy under ``root`` (real directories only)."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*.applied.docx"))


def _bytes(*paths: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in paths}


def _two_projects(tmp_path: Path) -> tuple[Path, Path]:
    return _spec(tmp_path / "projA"), _spec(tmp_path / "projB")


# ---------------------------------------------------------------------------
# Binding a name to a supplied file
# ---------------------------------------------------------------------------


class TestBinding:
    @pytest.mark.parametrize("order", ["A_then_B", "B_then_A"])
    def test_two_different_same_named_inputs_are_ambiguous(self, tmp_path, order):
        a, b = _two_projects(tmp_path)
        before = _bytes(a, b)
        inputs = [a, b] if order == "A_then_B" else [b, a]
        _, results = _run(tmp_path, [_edit("rf-1"), _edit("rf-2", target_element_id="p7", existing_text="scheduled")], inputs)
        (result,) = results
        assert {o.status for o in result.outcomes} == {OutcomeStatus.FILE_AMBIGUOUS}
        for outcome in result.outcomes:
            assert "spec.docx" in outcome.reason
            assert str(a) in outcome.reason and str(b) in outcome.reason
        assert sorted(result.candidate_paths) == sorted([str(a), str(b)])
        assert result.source_path == "" and result.output_path is None
        assert _copies(tmp_path) == []
        assert _bytes(a, b) == before

    def test_the_decision_and_its_wording_do_not_depend_on_input_order(self, tmp_path):
        a, b = _two_projects(tmp_path)

        def decisions(inputs):
            _, results = _run(tmp_path, [_edit("rf-1")], inputs)
            (result,) = results
            return (
                [(o.status, o.reason) for o in result.outcomes],
                result.candidate_paths,
                result.errors,
            )

        assert decisions([a, b]) == decisions([b, a])

    def test_the_index_holds_every_distinct_input_in_a_fixed_order(self, tmp_path):
        a, b = _two_projects(tmp_path)
        forward = run_module._index_specs([a, b, a])
        backward = run_module._index_specs([b, a, b])
        assert forward == backward
        assert set(forward["spec.docx"]) == {a, b}

    @pytest.mark.parametrize("spelling", ["same", "relative", "dotdot", "symlinked_dir"])
    def test_one_file_supplied_twice_is_one_input(self, tmp_path, monkeypatch, spelling):
        a = _spec(tmp_path / "projA")
        if spelling == "same":
            other = a
        elif spelling == "relative":
            monkeypatch.chdir(tmp_path)
            other = Path("projA") / "spec.docx"
        elif spelling == "dotdot":
            other = tmp_path / "projA" / ".." / "projA" / "spec.docx"
        else:
            link = tmp_path / "link"
            try:
                link.symlink_to(tmp_path / "projA", target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                pytest.skip(f"cannot create a symlink here: {exc}")
            other = link / "spec.docx"
        _, results = _run(tmp_path, [_edit("rf-1")], [a, other])
        assert _by_id(results)["rf-1"].status is OutcomeStatus.APPLIED
        assert (tmp_path / "projA" / "spec.applied.docx").exists()
        assert _copies(tmp_path) == [os.path.join("projA", "spec.applied.docx")]

    def test_a_case_variant_spelling_of_one_file_is_one_input(self, tmp_path):
        """What a case-insensitive volume (macOS) presents: one file under two
        spellings that ``Path.resolve`` does not unify. Reproduced with a hard
        link, which is one file too, so it runs on a case-sensitive CI disk."""
        upper = _spec(tmp_path / "proj", "Spec.docx")
        lower = tmp_path / "proj" / "spec.docx"
        try:
            os.link(upper, lower)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"no second spelling of one file here: {exc}")
        _, results = _run(tmp_path, [_edit("rf-1")], [upper, lower])
        assert _by_id(results)["rf-1"].status is OutcomeStatus.APPLIED
        assert len(_copies(tmp_path)) == 1

    @pytest.mark.parametrize("inode, same", [(0, False), (41, True)])
    def test_a_zero_inode_never_confirms_one_file(self, tmp_path, monkeypatch, inode, same):
        """Some filesystems report inode 0 for every file; trusting that would
        make two different files one input — the first-wins defect again."""
        probes = {tmp_path / "Spec.docx", tmp_path / "spec.docx"}
        first, second = (run_module._SuppliedInput(p, str(p)) for p in sorted(probes))
        real_stat = os.stat

        def fake_stat(path, *args, **kwargs):
            if Path(path) in probes:
                return SimpleNamespace(st_ino=inode, st_dev=7)
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(run_module.os, "stat", fake_stat)
        assert run_module._is_same_input(first, second) is same

    def test_two_different_files_differing_only_in_case_are_ambiguous(self, tmp_path):
        upper = _spec(tmp_path / "proj", "Spec.docx")
        lower = tmp_path / "proj" / "spec.docx"
        if lower.exists():
            pytest.skip("case-insensitive filesystem: the two names are one file")
        _spec(tmp_path / "proj", "spec.docx")
        _, results = _run(tmp_path, [_edit("rf-1")], [upper, lower])
        assert _by_id(results)["rf-1"].status is OutcomeStatus.FILE_AMBIGUOUS
        assert _copies(tmp_path) == []

    def test_a_sidecar_spelling_one_name_two_ways_edits_neither(self, tmp_path):
        a = _spec(tmp_path / "projA")
        _, results = _run(
            tmp_path,
            [_edit("rf-1", "spec.docx"), _edit("rf-2", "SPEC.docx", target_element_id="p7", existing_text="scheduled")],
            [a],
        )
        (result,) = results
        assert {o.status for o in result.outcomes} == {OutcomeStatus.FILE_AMBIGUOUS}
        assert all("'spec.docx'" in o.reason and "'SPEC.docx'" in o.reason for o in result.outcomes)
        assert _copies(tmp_path) == []

    @pytest.mark.parametrize("kind", ["relative", "windows", "absolute"])
    def test_a_path_in_the_sidecar_never_selects_a_file(self, tmp_path, kind):
        """A sidecar names files; it is not authority to open or overwrite a
        path — not even the exact path of a supplied specification."""
        a = _spec(tmp_path / "projA")
        name = {"relative": "projA/spec.docx", "windows": "projA\\spec.docx", "absolute": str(a)}[kind]
        before = _bytes(a)
        _, results = _run(tmp_path, [_edit("rf-1", name)], [a])
        outcome = _by_id(results)["rf-1"]
        assert outcome.status is OutcomeStatus.FILE_MISSING
        assert "names a path" in outcome.reason
        assert _copies(tmp_path) == []
        assert _bytes(a) == before

    def test_a_uniquely_bound_file_is_still_edited(self, tmp_path):
        a, b = _two_projects(tmp_path)
        other = _spec(tmp_path / "projA", "230000.docx")
        _, results = _run(
            tmp_path, [_edit("rf-held"), _edit("rf-ok", "230000.docx")], [a, b, other]
        )
        by_id = _by_id(results)
        assert by_id["rf-held"].status is OutcomeStatus.FILE_AMBIGUOUS
        assert by_id["rf-ok"].status is OutcomeStatus.APPLIED
        assert _copies(tmp_path) == [os.path.join("projA", "230000.applied.docx")]


# ---------------------------------------------------------------------------
# Destinations, decided before any write
# ---------------------------------------------------------------------------


class TestDestinations:
    def test_a_copy_that_would_overwrite_another_supplied_spec_is_refused(self, tmp_path):
        x = _spec(tmp_path, "x.docx")
        x_v2 = _spec(tmp_path, "x.v2.docx")  # supplied, not named by the sidecar
        before = _bytes(x, x_v2)
        _, results = _run(
            tmp_path, [_edit("rf-x", "x.docx")], [x, x_v2], RunSettings(output_suffix=".v2")
        )
        outcome = _by_id(results)["rf-x"]
        assert outcome.status is OutcomeStatus.DESTINATION_CONFLICT
        assert str(x_v2) in outcome.reason and "supplied as a specification" in outcome.reason
        assert _bytes(x, x_v2) == before

    def test_no_write_lands_on_an_input_a_later_document_reads(self, tmp_path):
        """x.docx's copy would be x.v2.docx — an input edited later in the same
        run. Deciding per file, the first write replaced x.v2.docx before it was
        read, so the run edited its own output. Planned up front, x.docx is
        refused and x.v2.docx is edited from the reviewed original."""
        x = _spec(tmp_path, "x.docx")
        x_v2 = _spec(tmp_path, "x.v2.docx")
        original = x_v2.read_bytes()
        _, results = _run(
            tmp_path,
            [_edit("rf-x", "x.docx"), _edit("rf-v2", "x.v2.docx")],
            [x, x_v2],
            RunSettings(output_suffix=".v2"),
        )
        by_id = _by_id(results)
        assert by_id["rf-x"].status is OutcomeStatus.DESTINATION_CONFLICT
        assert by_id["rf-v2"].status is OutcomeStatus.APPLIED
        assert x_v2.read_bytes() == original
        copy = extract_text_from_docx(tmp_path / "x.v2.v2.docx")
        assert copy.paragraph_map[4].text == "A. Submit product data before ordering materials."

    def test_two_copies_that_would_collide_are_both_held(self, tmp_path):
        """The check is on where the copies land, however that happens. A
        suffix that climbs out of a per-file folder sends both to one file."""
        a = _spec(tmp_path, "a.docx")
        b = _spec(tmp_path, "b.docx")
        _, results = _run(
            tmp_path,
            [_edit("rf-a", "a.docx"), _edit("rf-b", "b.docx")],
            [a, b],
            RunSettings(output_suffix="/../edited"),
        )
        by_id = _by_id(results)
        assert by_id["rf-a"].status is OutcomeStatus.DESTINATION_CONFLICT
        assert by_id["rf-b"].status is OutcomeStatus.DESTINATION_CONFLICT
        assert str(b) in by_id["rf-a"].reason and str(a) in by_id["rf-b"].reason
        assert not (tmp_path / "edited.docx").exists()

    @staticmethod
    def _hard_link(target: Path, link: Path) -> None:
        try:
            os.link(target, link)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"cannot create a hard link here: {exc}")

    def test_a_destination_hard_linked_to_a_supplied_spec_is_refused(self, tmp_path):
        """Saving rewrites the destination in place, so an existing copy that is
        a hard link to a supplied specification *is* that specification — no
        path comparison shows it (Codex review of PR #376)."""
        source = _spec(tmp_path, "spec.docx")
        other = _spec(tmp_path, "other.docx")
        self._hard_link(other, tmp_path / "spec.applied.docx")
        before = _bytes(source, other)
        _, results = _run(tmp_path, [_edit("rf-1")], [source, other])
        outcome = _by_id(results)["rf-1"]
        assert outcome.status is OutcomeStatus.DESTINATION_CONFLICT
        assert str(other) in outcome.reason and "another name" in outcome.reason
        assert _bytes(source, other) == before

    def test_a_destination_hard_linked_to_its_own_source_is_refused(self, tmp_path):
        source = _spec(tmp_path, "spec.docx")
        self._hard_link(source, tmp_path / "spec.applied.docx")
        before = _bytes(source)
        _, results = _run(tmp_path, [_edit("rf-1")], [source])
        outcome = _by_id(results)["rf-1"]
        assert outcome.status is OutcomeStatus.DESTINATION_CONFLICT
        assert "refusing to write over the source specification" in outcome.reason
        assert _bytes(source) == before

    def test_destinations_that_are_one_file_under_two_names_collide(self, tmp_path):
        a = _spec(tmp_path, "a.docx")
        b = _spec(tmp_path, "b.docx")
        (tmp_path / "a.applied.docx").write_bytes(b"an earlier copy")
        self._hard_link(tmp_path / "a.applied.docx", tmp_path / "b.applied.docx")
        _, results = _run(tmp_path, [_edit("rf-a", "a.docx"), _edit("rf-b", "b.docx")], [a, b])
        by_id = _by_id(results)
        assert by_id["rf-a"].status is OutcomeStatus.DESTINATION_CONFLICT
        assert by_id["rf-b"].status is OutcomeStatus.DESTINATION_CONFLICT
        assert (tmp_path / "a.applied.docx").read_bytes() == b"an earlier copy"

    def test_control_an_unrelated_existing_copy_is_still_replaced(self, tmp_path):
        """File identity is compared only against what was supplied: a stale
        copy that is nobody else's file is replaced, as it always was."""
        source = _spec(tmp_path, "spec.docx")
        stale = tmp_path / "spec.applied.docx"
        stale.write_bytes(b"an earlier copy")
        _, results = _run(tmp_path, [_edit("rf-1")], [source])
        assert _by_id(results)["rf-1"].status is OutcomeStatus.APPLIED
        assert stale.read_bytes() != b"an earlier copy"

    def test_output_dir_holding_its_own_sources_is_refused(self, tmp_path):
        a = _spec(tmp_path / "specs")
        before = _bytes(a)
        _, results = _run(
            tmp_path,
            [_edit("rf-1")],
            [a],
            RunSettings(output_dir=tmp_path / "specs", output_suffix=""),
        )
        outcome = _by_id(results)["rf-1"]
        assert outcome.status is OutcomeStatus.DESTINATION_CONFLICT
        assert "refusing to write over the source specification" in outcome.reason
        assert _bytes(a) == before

    def test_several_files_into_one_output_dir(self, tmp_path):
        names = ["210500.docx", "211313.docx", "230500.docx"]
        inputs = [_spec(tmp_path / f"folder{i}", name) for i, name in enumerate(names)]
        out = tmp_path / "out"
        _, results = _run(
            tmp_path,
            [_edit(f"rf-{i}", name) for i, name in enumerate(names)],
            inputs,
            RunSettings(output_dir=out),
        )
        assert {o.status for o in _by_id(results).values()} == {OutcomeStatus.APPLIED}
        assert sorted(p.name for p in out.iterdir()) == [
            "210500.applied.docx",
            "211313.applied.docx",
            "230500.applied.docx",
        ]

    def test_same_named_files_bound_for_one_output_dir_never_reach_it(self, tmp_path):
        a, b = _two_projects(tmp_path)
        out = tmp_path / "out"
        _, results = _run(tmp_path, [_edit("rf-1")], [a, b], RunSettings(output_dir=out))
        assert _by_id(results)["rf-1"].status is OutcomeStatus.FILE_AMBIGUOUS
        assert not out.exists()

    @pytest.mark.parametrize("name", ["same_path", "hard_link", "another_supplied"])
    def test_the_writer_refuses_a_supplied_destination_even_without_planning(
        self, tmp_path, name
    ):
        """The last line of defense before the one irreversible step: the
        source under its own name or another, or any other supplied file."""
        x = _spec(tmp_path, "x.docx")
        protected, destination = run_module._Protected([]), x
        if name == "hard_link":
            destination = tmp_path / "x.applied.docx"
            self._hard_link(x, destination)
        elif name == "another_supplied":
            destination = _spec(tmp_path, "other.docx")
            protected = run_module._Protected([x, destination])
        before = _bytes(x, destination)
        sidecar = load_sidecar(_write_sidecar(tmp_path, [_edit("rf-x", "x.docx")]))
        result = FileResult(file_name="x.docx")
        run_module._apply_to_file(
            x,
            list(sidecar.entries),
            result,
            RunSettings(),
            destination=destination,
            protected=protected,
            client=None,
            log=lambda _message: None,
        )
        (outcome,) = result.outcomes
        assert outcome.status is OutcomeStatus.FAILED
        assert "refusing to write over a supplied specification" in outcome.reason
        assert result.applied == 0 and result.output_path is None
        assert _bytes(x, destination) == before


# ---------------------------------------------------------------------------
# Receipt, summary, and exit status
# ---------------------------------------------------------------------------


class TestReceiptAndExitStatus:
    def test_the_receipt_accounts_for_every_held_instruction(self, tmp_path):
        a, b = _two_projects(tmp_path)
        other = _spec(tmp_path / "projA", "230000.docx")
        edits = [
            _edit("rf-held-1"),
            _edit("rf-held-2", target_element_id="p7", existing_text="scheduled"),
            _edit("rf-ok", "230000.docx"),
        ]
        sidecar, results = _run(tmp_path, edits, [a, b, other])
        receipt = build_receipt(
            sidecar=sidecar,
            file_results=results,
            settings=RunSettings().to_dict(),
            entries_in=len(sidecar.entries) + len(sidecar.malformed),
        )
        accounting = receipt["accounting"]
        assert accounting["balanced"] is True
        assert accounting["by_outcome"]["FILE_AMBIGUOUS"] == 2
        assert accounting["by_outcome"]["APPLIED"] == 1
        held = next(f for f in receipt["files"] if f["file_name"] == "spec.docx")
        assert sorted(held["candidate_paths"]) == sorted([str(a), str(b)])
        assert all(o["outcome"] == "FILE_AMBIGUOUS" and o["reason"] for o in held["outcomes"])
        summary = render_summary(receipt, results)
        assert "2 file ambiguous" in summary
        assert summary.index("Not applied:") < summary.index("Applied:")

    def _cli(self, tmp_path, *extra):
        sidecar = _write_sidecar(tmp_path, [_edit("rf-held"), _edit("rf-ok", "230000.docx")])
        argv = [str(sidecar), "--specs", str(tmp_path / "projA"), "--specs", str(tmp_path / "projB"), *extra]
        return sidecar, main(argv)

    @pytest.mark.parametrize("strict", [False, True])
    def test_an_ambiguous_input_exits_3_with_or_without_strict(self, tmp_path, capsys, strict):
        _two_projects(tmp_path)
        _spec(tmp_path / "projA", "230000.docx")
        _, code = self._cli(tmp_path, *(["--strict"] if strict else []))
        assert code == EXIT_INPUT_HELD == 3
        assert "held" in capsys.readouterr().err
        receipt = json.loads((tmp_path / "report.edits.applied.json").read_text(encoding="utf-8"))
        assert receipt["accounting"]["balanced"] is True
        assert receipt["accounting"]["by_outcome"]["FILE_AMBIGUOUS"] == 1
        # The uniquely bound document was still processed.
        assert _copies(tmp_path) == [os.path.join("projA", "230000.applied.docx")]

    def test_a_destination_conflict_exits_3(self, tmp_path):
        """The re-run case: ``--specs <dir>`` sweeps in last run's edited copy,
        which may hold a reviewer's own work by now. It is never overwritten."""
        _spec(tmp_path, "spec.docx")
        previous = _spec(tmp_path, "spec.applied.docx")
        before = previous.read_bytes()
        sidecar = _write_sidecar(tmp_path, [_edit("rf-1")])
        assert main([str(sidecar), "--specs", str(tmp_path)]) == EXIT_INPUT_HELD
        assert previous.read_bytes() == before

    def test_a_clean_run_still_exits_0(self, tmp_path):
        _spec(tmp_path / "projA")
        sidecar = _write_sidecar(tmp_path, [_edit("rf-1")])
        assert main([str(sidecar), "--specs", str(tmp_path / "projA")]) == EXIT_OK


# ---------------------------------------------------------------------------
# --assist and --dry-run
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Counts assist calls; answers with no tool call, so assist declines."""

    def __init__(self):
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[])


_ASSIST = RunSettings(assist=AssistConfig(enabled=True, model="test-model"))


class TestAssistAndDryRun:
    def test_control_assist_is_consulted_inside_a_bound_document(self, tmp_path):
        a = _spec(tmp_path / "projA")
        client = _RecordingClient()
        _, results = _run(
            tmp_path, [_edit("rf-1", **_IN_DOCUMENT_AMBIGUITY)], [a], _ASSIST, client=client
        )
        assert _by_id(results)["rf-1"].status is OutcomeStatus.UNLOCATED
        assert len(client.calls) == 1

    @pytest.mark.parametrize("hold", ["ambiguous_file", "destination_conflict"])
    def test_assist_is_never_asked_about_a_held_document(self, tmp_path, hold):
        client = _RecordingClient()
        if hold == "ambiguous_file":
            inputs, settings = list(_two_projects(tmp_path)), _ASSIST
            expected = OutcomeStatus.FILE_AMBIGUOUS
        else:
            inputs = [_spec(tmp_path / "projA")]
            settings = RunSettings(assist=_ASSIST.assist, output_suffix="")
            expected = OutcomeStatus.DESTINATION_CONFLICT
        _, results = _run(
            tmp_path, [_edit("rf-1", **_IN_DOCUMENT_AMBIGUITY)], inputs, settings, client=client
        )
        assert _by_id(results)["rf-1"].status is expected
        assert client.calls == []

    def test_a_dry_run_makes_the_same_decisions_as_a_real_run(self, tmp_path):
        inputs = [
            *_two_projects(tmp_path),  # spec.docx: ambiguous
            _spec(tmp_path / "c", "x.docx"),  # x.docx: copy would overwrite x.v2.docx
            _spec(tmp_path / "c", "x.v2.docx"),
            _spec(tmp_path / "d", "230000.docx"),  # clean
        ]
        edits = [_edit("rf-amb"), _edit("rf-x", "x.docx"), _edit("rf-ok", "230000.docx")]
        settings = RunSettings(output_suffix=".v2")

        def decisions(dry_run: bool) -> dict:
            _, results = _run(
                tmp_path,
                edits,
                inputs,
                RunSettings(output_suffix=settings.output_suffix, dry_run=dry_run),
            )
            normalized = {OutcomeStatus.WOULD_APPLY: OutcomeStatus.APPLIED}
            return {
                fid: (normalized.get(o.status, o.status), o.reason)
                for fid, o in _by_id(results).items()
            }

        dry = decisions(True)
        assert not (tmp_path / "d" / "230000.v2.docx").exists()
        wet = decisions(False)
        assert dry == wet
        assert {fid: status for fid, (status, _) in wet.items()} == {
            "rf-amb": OutcomeStatus.FILE_AMBIGUOUS,
            "rf-x": OutcomeStatus.DESTINATION_CONFLICT,
            "rf-ok": OutcomeStatus.APPLIED,
        }
