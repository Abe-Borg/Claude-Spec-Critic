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


def _submission(basis: dict | None) -> BatchSubmission:
    return BatchSubmission(
        job=BatchJob(
            batch_id="msgbatch_test",
            job_type="review",
            request_map={"spec-0": {"file": "21 13 13.docx"}},
            created_at=1_700_000_000.0,
        ),
        files_reviewed=["21 13 13.docx"],
        review_request_ids=["spec-0"],
        module_id="datacenter_fire",
        cycle_label=get_module("datacenter_fire").cycle.label,
        governing_basis=basis,
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

    def test_a_profileless_submission_persists_no_basis_key_value(self):
        pending = PendingBatch.from_submission(_submission(None))
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
