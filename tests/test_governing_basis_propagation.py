"""Pins for carrying the governing basis through submission, save and resume.

Plan step 2, sections 5.6 / 5.7 — the *plumbing* sub-chunk. The basis is built
once before any review spend and carried unchanged; nothing renders it into a
prompt yet and nothing folds it into a cache key, so this change must be
**behaviourally inert**. Two properties matter and are pinned separately:

1. **It survives.** A resumed run must ask the question it originally paid for,
   so the basis is persisted and restored verbatim — never rebuilt from today's
   module data, which would silently answer a different question.
2. **It changes nothing else.** Every profile-less path (the California module,
   every run today) carries ``None`` end to end, which is what makes the
   byte-identical claim structural rather than hopeful.

Recovery honesty is the third: a bare-id recovery has no snapshot, and saying
so is the whole point — inventing one from current pins would be the same
failure the basis exists to prevent, committed during recovery.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.batch.batch import BatchJob
from src.core.project_profile import ProjectProfile
from src.modules.registry import AVAILABLE_MODULES, get_module
from src.orchestration.batch_resume import (
    PendingBatch,
    load_pending_batch,
    save_pending_batch,
)
from src.orchestration.pipeline import (
    BatchSubmission,
    PreparedBatchReview,
    _PreparedSpecs,
    _batch_submission_from_prepared,
    build_realtime_batch_submission,
    build_run_governing_basis,
)
from src.verification.governing_context import (
    MODE_PROVENANCE_ONLY,
    MODE_RESEARCHED_CONTEXT,
    RESEARCH_STATE_RECOVERED,
    basis_from_dict,
)

_RESEARCH = {
    "items": [
        {
            "item_id": "r-1",
            "dimension_id": "adoption",
            "category": "governing_code",
            "topic": "Sprinkler standard edition",
            "requirement": "NFPA 13-2019 applies via the 2021 USBC.",
            "authority": "Virginia USBC",
            "code_reference": "13VAC5-63",
            "grounded": True,
            "accepted_sources": ["https://law.example/usbc"],
            "confidence": 0.9,
            "actionability": "spec_requirement",
            "notes": "Applies to buildings over 75 ft.",
        }
    ],
    "dimension_statuses": [
        {"dimension_id": "adoption", "status": "completed", "item_count": 1}
    ],
    "research_date": "2026-09-09",
}


def _profile() -> ProjectProfile:
    return ProjectProfile(
        city="Ashburn", state_or_province="VA", country="US", client_name="Acme"
    )


def _submission(
    basis: dict | None,
    *,
    module_id: str = "datacenter_fire",
    requirements_profile: dict | None = None,
    project_profile: dict | None = None,
) -> BatchSubmission:
    return BatchSubmission(
        job=BatchJob(
            batch_id="msgbatch_test",
            job_type="review",
            request_map={"spec-0": {"file": "21 13 13.docx"}},
            created_at=1_700_000_000.0,
        ),
        files_reviewed=["21 13 13.docx"],
        review_request_ids=["spec-0"],
        module_id=module_id,
        cycle_label=get_module(module_id).cycle.label,
        governing_basis=basis,
        requirements_profile=requirements_profile,
        project_profile=project_profile,
    )


class TestBuiltOnceBeforeSpend:
    def test_a_profile_enabled_module_gets_a_researched_basis(self):
        basis = build_run_governing_basis(
            module=get_module("datacenter_fire"),
            project_profile=_profile(),
            requirements_profile=_RESEARCH,
        )
        assert basis is not None
        assert basis["mode"] == MODE_RESEARCHED_CONTEXT
        assert basis["items"][0]["requirement"] == (
            "NFPA 13-2019 applies via the 2021 USBC."
        )

    def test_a_profile_enabled_module_without_research_still_gets_a_basis(self):
        """Provenance-only is a disclosure, not a degraded path.

        This is the run where the module's pins are the *only* thing carried,
        so it is where presenting them as unqualified authority does the most
        damage — and therefore where the basis is most needed.
        """
        basis = build_run_governing_basis(
            module=get_module("datacenter_fire"),
            project_profile=None,
            requirements_profile=None,
        )
        assert basis is not None
        assert basis["mode"] == MODE_PROVENANCE_ONLY
        assert any("marked UNVERIFIED" in o for o in basis["omissions"])

    def test_the_project_identity_is_snapshotted(self):
        basis = build_run_governing_basis(
            module=get_module("datacenter_fire"),
            project_profile=_profile(),
            requirements_profile=_RESEARCH,
        )
        assert basis["project"] == {
            "city": "Ashburn",
            "state_or_province": "VA",
            "country": "US",
            "client_name": "Acme",
        }

    def test_the_serialized_form_round_trips_to_the_same_identity(self):
        basis = build_run_governing_basis(
            module=get_module("datacenter_fire"),
            project_profile=_profile(),
            requirements_profile=_RESEARCH,
        )
        assert basis_from_dict(basis).fingerprint() == basis["fingerprint"]


class TestReachesTheSubmission:
    """The prepared -> submission hop, exercised through the real constructor.

    Building a ``BatchSubmission`` directly in a test proves nothing about this
    link: ``_batch_submission_from_prepared`` is the single place both the batch
    wrapper and the program-level realtime scheduler materialize a child
    submission, so a field dropped there is dropped for every transport at once.
    """

    def _prepared(self, module_id: str, basis: dict | None) -> PreparedBatchReview:
        return PreparedBatchReview(
            module=get_module(module_id),
            prepared=_PreparedSpecs(specs=[], leed_alerts=[], placeholder_alerts=[]),
            effective_context="ctx",
            requirements_profile=None,
            project_profile=None,
            model="m",
            cross_check_enabled=False,
            review_transport="batch",
            governing_basis=basis,
        )

    def _job(self) -> BatchJob:
        return BatchJob(
            batch_id="msgbatch_test",
            job_type="review",
            request_map={},
            created_at=1_700_000_000.0,
        )

    def test_the_basis_survives_the_prepared_to_submission_hop(self):
        basis = build_run_governing_basis(
            module=get_module("datacenter_fire"),
            project_profile=_profile(),
            requirements_profile=_RESEARCH,
        )
        submission = _batch_submission_from_prepared(
            self._prepared("datacenter_fire", basis), job=self._job()
        )
        assert submission.governing_basis == basis

    def test_the_realtime_transport_carries_it_too(self):
        """Both transports go through the same constructor; pin that they do."""
        basis = build_run_governing_basis(
            module=get_module("datacenter_fire"),
            project_profile=_profile(),
            requirements_profile=_RESEARCH,
        )
        prepared = self._prepared("datacenter_fire", basis)
        prepared = replace(prepared, review_transport="realtime")
        submission = build_realtime_batch_submission(
            prepared, realtime_results={}, request_map={}, started_at=1_700_000_000.0
        )
        assert submission.governing_basis == basis

    def test_a_profileless_prepared_run_produces_no_basis_on_the_submission(self):
        submission = _batch_submission_from_prepared(
            self._prepared("california_k12_mep", None), job=self._job()
        )
        assert submission.governing_basis is None


class TestProfilelessRunsCarryNothing:
    """The structural basis of the byte-identical claim."""

    def test_the_california_module_gets_no_basis(self):
        assert (
            build_run_governing_basis(
                module=get_module("california_k12_mep"),
                project_profile=None,
                requirements_profile=None,
            )
            is None
        )

    def test_a_profile_and_research_do_not_force_one_onto_california(self):
        """The gate is the module's capability flag, not the data available.

        Keying off "is there research?" would let an unrelated change hand the
        California path a basis it has no branch for.
        """
        assert (
            build_run_governing_basis(
                module=get_module("california_k12_mep"),
                project_profile=_profile(),
                requirements_profile=_RESEARCH,
            )
            is None
        )

    def test_exactly_the_profile_enabled_modules_produce_a_basis(self):
        for module_id, module in sorted(AVAILABLE_MODULES.items()):
            basis = build_run_governing_basis(
                module=module, project_profile=None, requirements_profile=None
            )
            expected = getattr(module, "project_profile_enabled", False)
            assert (basis is not None) is expected, module_id

    def test_a_profileless_submission_carries_none_through_resume(self):
        """``None`` here means "no basis concept", and must stay that way.

        The module is the California one on purpose: a data-center record with
        no saved basis is a *lost* basis, which resolves to a recovered one —
        a different case entirely, covered in TestSavedBasisResolution.
        """
        pending = PendingBatch.from_submission(
            _submission(None, module_id="california_k12_mep")
        )
        assert pending.governing_basis is None
        assert pending.to_submission().governing_basis is None


class TestSurvivesSaveAndResume:
    def test_the_basis_round_trips_through_disk_verbatim(self, tmp_path: Path):
        basis = build_run_governing_basis(
            module=get_module("datacenter_fire"),
            project_profile=_profile(),
            requirements_profile=_RESEARCH,
        )
        target = tmp_path / "pending_batch.json"
        assert save_pending_batch(PendingBatch.from_submission(_submission(basis)), path=target)

        loaded = load_pending_batch(path=target)
        assert loaded is not None
        assert loaded.governing_basis == basis
        assert loaded.to_submission().governing_basis == basis

    def test_resume_preserves_the_original_identity(self, tmp_path: Path):
        """The property the whole snapshot exists for."""
        basis = build_run_governing_basis(
            module=get_module("datacenter_fire"),
            project_profile=_profile(),
            requirements_profile=_RESEARCH,
        )
        target = tmp_path / "pending_batch.json"
        save_pending_batch(PendingBatch.from_submission(_submission(basis)), path=target)
        restored = load_pending_batch(path=target).to_submission()
        assert (
            basis_from_dict(restored.governing_basis).fingerprint()
            == basis["fingerprint"]
        )

    def test_resume_does_not_rebuild_from_todays_module_data(self, tmp_path: Path):
        """A saved run whose module pins later changed keeps its own basis.

        Rebuilding would hand a resumed run assumptions it never reviewed
        under — the same class of error as re-running research on resume.
        """
        basis = build_run_governing_basis(
            module=get_module("datacenter_fire"),
            project_profile=_profile(),
            requirements_profile=_RESEARCH,
        )
        stale = json.loads(json.dumps(basis))
        stale["module_basis"]["base_codes"] = [["ibc", "IBC", "2018"]]
        target = tmp_path / "pending_batch.json"
        save_pending_batch(PendingBatch.from_submission(_submission(stale)), path=target)

        restored = load_pending_batch(path=target).to_submission()
        assert restored.governing_basis["module_basis"]["base_codes"] == [
            ["ibc", "IBC", "2018"]
        ]
        assert restored.governing_basis["module_basis"]["base_codes"] != (
            basis["module_basis"]["base_codes"]
        )

    def test_a_legacy_state_file_without_the_key_loads(self, tmp_path: Path):
        """Additive field, defensive load — no schema bump."""
        target = tmp_path / "pending_batch.json"
        save_pending_batch(PendingBatch.from_submission(_submission(None)), path=target)
        raw = json.loads(target.read_text(encoding="utf-8"))
        raw.pop("governing_basis", None)
        target.write_text(json.dumps(raw), encoding="utf-8")

        loaded = load_pending_batch(path=target)
        assert loaded is not None
        assert loaded.governing_basis is None

    @pytest.mark.parametrize("garbage", ["not-a-dict", 42, [], True])
    def test_a_malformed_saved_basis_degrades_to_none(self, tmp_path: Path, garbage):
        """Never a startup failure; a paid review stays recoverable."""
        target = tmp_path / "pending_batch.json"
        save_pending_batch(PendingBatch.from_submission(_submission(None)), path=target)
        raw = json.loads(target.read_text(encoding="utf-8"))
        raw["governing_basis"] = garbage
        target.write_text(json.dumps(raw), encoding="utf-8")

        loaded = load_pending_batch(path=target)
        assert loaded is not None
        assert loaded.governing_basis is None


class TestSavedBasisResolution:
    """``None`` must never be able to mean "we lost one" (plan section 5.7).

    A saved record that silently reads as no-context would let a resumed
    data-center run present as though no research had ever been done, and after
    activation that is a verification run answering a question nobody asked.
    So every path that cannot produce the *original* basis produces a visibly
    recovered one instead — never ``None``, never a fresh basis wearing the
    original's clothes.
    """

    def _resolved(self, pending: PendingBatch) -> dict | None:
        return pending.to_submission().governing_basis

    def test_a_valid_saved_snapshot_is_used_verbatim(self):
        basis = build_run_governing_basis(
            module=get_module("datacenter_fire"),
            project_profile=_profile(),
            requirements_profile=_RESEARCH,
        )
        resolved = self._resolved(PendingBatch.from_submission(_submission(basis)))
        assert resolved == basis
        assert resolved["research_state"] != RESEARCH_STATE_RECOVERED

    def test_a_legacy_record_recovers_its_saved_research(self):
        """The facts were saved; only the pins are unreconstructable.

        Discarding the research too would make a run that did real work
        indistinguishable from one that did none.
        """
        resolved = self._resolved(
            PendingBatch.from_submission(
                _submission(
                    None,
                    requirements_profile=_RESEARCH,
                    project_profile={"city": "Ashburn", "state_or_province": "VA"},
                )
            )
        )
        assert resolved is not None
        assert resolved["research_state"] == RESEARCH_STATE_RECOVERED
        assert resolved["items"], "saved research facts were discarded"
        assert resolved["items"][0]["requirement"] == (
            "NFPA 13-2019 applies via the 2021 USBC."
        )
        assert resolved["project"]["city"] == "Ashburn"
        assert any("TODAY's values" in o for o in resolved["omissions"])

    def test_a_legacy_record_without_research_is_still_marked_recovered(self):
        resolved = self._resolved(PendingBatch.from_submission(_submission(None)))
        assert resolved is not None
        assert resolved["research_state"] == RESEARCH_STATE_RECOVERED
        assert resolved["items"] == []

    def test_an_unsupported_policy_version_does_not_pass_through(self):
        """``isinstance(dict)`` is a shape check, not a validity check."""
        basis = build_run_governing_basis(
            module=get_module("datacenter_fire"),
            project_profile=_profile(),
            requirements_profile=_RESEARCH,
        )
        stale = json.loads(json.dumps(basis))
        stale["policy_version"] = basis["policy_version"] + 1
        resolved = self._resolved(PendingBatch.from_submission(_submission(stale)))
        assert resolved["research_state"] == RESEARCH_STATE_RECOVERED
        assert any("not readable by this build" in o for o in resolved["omissions"])

    def test_another_modules_snapshot_is_refused(self):
        """The mismatch that would apply the wrong module's assumptions."""
        foreign = build_run_governing_basis(
            module=get_module("datacenter_electrical"),
            project_profile=_profile(),
            requirements_profile=_RESEARCH,
        )
        pending = PendingBatch.from_submission(
            _submission(foreign, module_id="datacenter_fire")
        )
        resolved = self._resolved(pending)
        assert resolved["module_basis"]["module_id"] == "datacenter_fire"
        assert resolved["research_state"] == RESEARCH_STATE_RECOVERED
        assert any("belongs to module" in o for o in resolved["omissions"])

    def test_a_malformed_snapshot_degrades_rather_than_raising(self):
        broken = {"schema_version": 1, "policy_version": 1, "items": "not-a-list"}
        resolved = self._resolved(PendingBatch.from_submission(_submission(broken)))
        assert resolved["research_state"] == RESEARCH_STATE_RECOVERED

    def test_every_degradation_is_logged(self):
        lines: list[str] = []
        pending = PendingBatch.from_submission(_submission(None))
        pending.to_submission(log=lines.append)
        assert any("Governing basis" in line for line in lines)

    def test_the_paid_results_survive_every_degradation(self):
        """A snapshot problem must never discard a recoverable paid review."""
        for saved in (None, {"schema_version": 99}, {"items": "bad"}):
            submission = PendingBatch.from_submission(
                _submission(saved)
            ).to_submission()
            assert submission.files_reviewed == ["21 13 13.docx"]
            assert submission.review_request_ids == ["spec-0"]

    def test_california_is_untouched_by_any_of_it(self):
        for saved in (None, {"schema_version": 99}, {"items": "bad"}):
            resolved = self._resolved(
                PendingBatch.from_submission(
                    _submission(saved, module_id="california_k12_mep")
                )
            )
            assert resolved is None


class TestProjectIdentityShapeAgreesAcrossPaths:
    """The two paths read the project identity differently — pin that they agree.

    ``build_run_governing_basis`` reads **attributes** off a live
    ``ProjectProfile``; the resume path hands ``recovered_basis`` the **dict**
    that was persisted. Both land in ``VerificationBasis.project``, which is
    inside the fingerprint. If the two shapes ever drift — a renamed field, a
    key the serializer stops emitting — a resumed run would carry a different
    project block from the original and silently take a different cache
    identity, which is the reuse the fingerprint exists to control.

    Checking this by hand once is what the rest of this file keeps proving is
    not enough.
    """

    def test_the_serializer_emits_exactly_the_keys_the_basis_reads(self):
        expected = {"city", "state_or_province", "country", "client_name"}
        assert set(_profile().to_dict()) == expected

    def test_both_paths_produce_the_same_project_mapping(self):
        from src.verification.governing_context import recovered_basis

        module = get_module("datacenter_fire")
        built = build_run_governing_basis(
            module=module, project_profile=_profile(), requirements_profile=_RESEARCH
        )
        resumed = recovered_basis(
            module.module_id,
            module.cycle,
            profile=_RESEARCH,
            project=_profile().to_dict(),
        ).to_dict()
        assert built["project"] == resumed["project"]

    def test_a_dropped_serializer_key_is_caught(self):
        """The failure this pins: the dict path silently loses a field."""
        module = get_module("datacenter_fire")
        from src.verification.governing_context import recovered_basis

        partial = _profile().to_dict()
        partial.pop("client_name")
        resumed = recovered_basis(
            module.module_id, module.cycle, profile=_RESEARCH, project=partial
        ).to_dict()
        built = build_run_governing_basis(
            module=module, project_profile=_profile(), requirements_profile=_RESEARCH
        )
        assert resumed["project"] != built["project"]


class TestRoutedProgramsKeepPerModuleBases:
    """Plan section 5.8: each module retains *its own* basis.

    The program manifest persists children through the same
    ``PendingBatch`` round trip, so this is covered by construction — which is
    exactly why it is worth running rather than reading. Two modules that pin
    different editions must not converge on one basis, and a manifest round
    trip must not let one module's assumptions land under another's id.
    """

    def _program(self):
        from src.orchestration.program_pipeline import ProgramSubmission
        from src.programs.assignments import SpecAssignment, SpecRoutingDecision
        from src.programs.routing import RoutingState

        module_ids = ("datacenter_fire", "datacenter_electrical")
        partitions = {}
        for module_id in module_ids:
            module = get_module(module_id)
            basis = build_run_governing_basis(
                module=module, project_profile=_profile(), requirements_profile=_RESEARCH
            )
            sub = _submission(basis)
            sub.module_id = module_id
            sub.cycle_label = module.cycle.label
            partitions[module_id] = sub

        assignments = (
            SpecAssignment(
                source_path="21 13 13 - Sprinklers.docx",
                decision=SpecRoutingDecision(
                    spec_id="21 13 13",
                    program_id="hyperscale_datacenter",
                    automatic_state=RoutingState.SUPPORTED,
                    automatic_module_ids=module_ids,
                    confidence=1.0,
                    evidence=(),
                ),
            ),
        )
        return ProgramSubmission(
            program_id="hyperscale_datacenter",
            assignments=assignments,
            partitions=partitions,
        )

    def test_each_module_carries_a_distinct_basis(self):
        program = self._program()
        bases = {
            mid: child.governing_basis for mid, child in program.partitions.items()
        }
        assert all(b is not None for b in bases.values())
        fingerprints = {b["fingerprint"] for b in bases.values()}
        assert len(fingerprints) == len(bases), (
            "two modules with different pins converged on one basis"
        )

    def test_each_basis_names_its_own_module(self):
        for module_id, child in self._program().partitions.items():
            assert child.governing_basis["module_basis"]["module_id"] == module_id

    def test_a_manifest_round_trip_keeps_them_apart(self, tmp_path: Path):
        from src.orchestration.batch_resume import (
            PendingProgramRun,
            load_pending_run,
            save_pending_program_run,
        )

        program = self._program()
        before = {
            mid: child.governing_basis for mid, child in program.partitions.items()
        }
        target = tmp_path / "pending_batch.json"
        assert save_pending_program_run(
            PendingProgramRun.from_submission(program), path=target
        )

        loaded = load_pending_run(path=target)
        assert loaded is not None
        after = {
            mid: _pending_child_basis(loaded, mid) for mid in before
        }
        assert after == before


def _pending_child_basis(pending_run, module_id: str) -> dict | None:
    from src.orchestration.batch_resume import _pending_batch_from_mapping

    return _pending_batch_from_mapping(pending_run.partitions[module_id]).governing_basis


class TestBareIdRecoveryIsHonest:
    """Plan section 5.7: never pretend the current context was the original."""

    def _thin(self, module_id: str, monkeypatch):
        """Drive the real bare-id path with the remote calls stubbed out.

        ``thin_submission_from_batch_results`` resolves the batch helper by a
        function-local import, so the patch has to land on the defining module,
        not on ``batch_resume``.
        """
        from src.batch import batch as batch_mod
        from src.orchestration import batch_resume

        captured: dict = {}

        def fake_reconstruct(**kwargs):
            captured.update(kwargs)
            return _submission(kwargs.get("governing_basis"))

        monkeypatch.setattr(batch_mod, "_collect_batch_results_with_retry", lambda *a, **k: {})
        monkeypatch.setattr(batch_resume, "reconstruct_batch_submission", fake_reconstruct)
        batch_resume.thin_submission_from_batch_results(
            "msgbatch_x", model="m", module=get_module(module_id)
        )
        return captured.get("governing_basis")

    def test_a_datacenter_bare_id_recovery_is_marked_recovered(self, monkeypatch):
        basis = self._thin("datacenter_fire", monkeypatch)
        assert basis is not None
        assert basis["research_state"] == RESEARCH_STATE_RECOVERED

    def test_it_says_todays_pins_are_not_the_originals(self, monkeypatch):
        joined = " ".join(self._thin("datacenter_fire", monkeypatch)["omissions"])
        assert "cannot be reconstructed" in joined
        assert "TODAY's values" in joined

    def test_a_california_bare_id_recovery_carries_no_basis(self, monkeypatch):
        assert self._thin("california_k12_mep", monkeypatch) is None
