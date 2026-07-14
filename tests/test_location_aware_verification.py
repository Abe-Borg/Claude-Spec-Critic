"""WS-4b: location-aware verification, cache jurisdiction segment, sidecar v4.

CA-neutrality byte-pins are the heart of this file: with no profile, the
web_search tool dict, the web_fetch tool dict, and the verification cache
key must be byte-identical to their pre-WS-4 shapes.
"""
from __future__ import annotations

import json

import pytest

from src.core.code_cycles import DEFAULT_CYCLE
from src.core.project_profile import ProjectProfile
from src.review.reviewer import Finding, ReviewResult
from src.verification.verification_cache import VerificationCache, make_cache_key
from src.verification.verification_routing import (
    build_verification_request,
    build_verification_tools_from_decision,
    select_routing,
)
from src.verification.verifier import VerificationResult


def _finding(**overrides) -> Finding:
    defaults = dict(
        severity="HIGH",
        fileName="21 13 13 Wet-Pipe.docx",
        section="2.1",
        issue="Cited edition is stale for the adopted cycle.",
        actionType="EDIT",
        existingText="2015 IBC",
        replacementText="2024 IBC",
        codeReference="IBC 2024",
        confidence=0.8,
    )
    defaults.update(overrides)
    return Finding(**defaults)


def _profile() -> ProjectProfile:
    return ProjectProfile(
        city="Markham", state_or_province="ON", country="CA", client_name="ExampleCo"
    )


_MARKHAM_LOCATION = {
    "type": "approximate",
    "country": "CA",
    "region": "Ontario",
    "city": "Markham",
}


class TestUserLocationThreading:
    def test_profile_less_tools_are_byte_identical(self):
        decision = select_routing(_finding(), cycle=DEFAULT_CYCLE)
        tools = build_verification_tools_from_decision(decision)
        baseline = build_verification_tools_from_decision(decision, user_location=None)
        assert tools == baseline
        web_search = tools[0]
        # The pre-WS-4 hardcoded default, byte-for-byte.
        assert web_search["user_location"] == {
            "type": "approximate",
            "country": "US",
            "region": "California",
        }

    def test_profile_location_lands_on_web_search_only(self):
        decision = select_routing(_finding(), cycle=DEFAULT_CYCLE)
        tools = build_verification_tools_from_decision(
            decision, user_location=_MARKHAM_LOCATION
        )
        by_name = {t.get("name"): t for t in tools}
        assert by_name["web_search"]["user_location"] == _MARKHAM_LOCATION
        # web_fetch has no location parameter — the key must NEVER appear.
        assert "user_location" not in by_name["web_fetch"]
        # The verdict tool is untouched too.
        assert "user_location" not in by_name["submit_verification_verdict"]

    def test_request_builder_threads_location(self):
        decision = select_routing(_finding(), cycle=DEFAULT_CYCLE)
        request = build_verification_request(
            decision,
            prompt="verify this",
            system_prompt="you verify",
            user_location=_MARKHAM_LOCATION,
        )
        web_search = request.params["tools"][0]
        assert web_search["user_location"] == _MARKHAM_LOCATION

    def test_request_without_location_is_byte_identical(self):
        decision = select_routing(_finding(), cycle=DEFAULT_CYCLE)
        with_default = build_verification_request(
            decision, prompt="verify this", system_prompt="you verify"
        )
        explicit_none = build_verification_request(
            decision,
            prompt="verify this",
            system_prompt="you verify",
            user_location=None,
        )
        assert with_default.params == explicit_none.params

    def test_location_inputs_for_submission(self):
        from src.orchestration.pipeline import location_inputs_for_submission

        class _Sub:
            project_profile = _profile().to_dict()

        location, fingerprint = location_inputs_for_submission(_Sub())
        assert location == _MARKHAM_LOCATION
        assert fingerprint == _profile().jurisdiction_fingerprint()

        class _NoProfile:
            project_profile = None

        assert location_inputs_for_submission(_NoProfile()) == (None, None)

        class _Incomplete:
            project_profile = {"city": "Markham"}

        assert location_inputs_for_submission(_Incomplete()) == (None, None)


class TestJurisdictionCacheKey:
    def test_profile_less_key_is_byte_identical_five_segments(self):
        finding = _finding()
        key = make_cache_key(finding, cycle=DEFAULT_CYCLE)
        explicit_none = make_cache_key(
            finding, cycle=DEFAULT_CYCLE, jurisdiction_fingerprint=None
        )
        assert key == explicit_none
        assert key.count("|") == 4  # five segments, no sixth
        # Exact legacy shape: label|std_fp|action|code_ref|claim_digest.
        assert key.startswith(f"{DEFAULT_CYCLE.label}|")

    def test_fingerprint_appends_sixth_segment(self):
        finding = _finding()
        fp = _profile().jurisdiction_fingerprint()
        key = make_cache_key(
            finding, cycle=DEFAULT_CYCLE, jurisdiction_fingerprint=fp
        )
        base = make_cache_key(finding, cycle=DEFAULT_CYCLE)
        assert key == f"{base}|{fp}"

    def test_different_cities_produce_different_keys(self):
        finding = _finding()
        markham = ProjectProfile("Markham", "ON", "CA", "ExampleCo")
        ashburn = ProjectProfile("Ashburn", "VA", "US", "ExampleCo")
        key_a = make_cache_key(
            finding,
            cycle=DEFAULT_CYCLE,
            jurisdiction_fingerprint=markham.jurisdiction_fingerprint(),
        )
        key_b = make_cache_key(
            finding,
            cycle=DEFAULT_CYCLE,
            jurisdiction_fingerprint=ashburn.jurisdiction_fingerprint(),
        )
        assert key_a != key_b

    def test_cache_isolation_across_jurisdictions(self):
        cache = VerificationCache()
        finding = _finding()
        result = VerificationResult(
            verdict="CONFIRMED",
            explanation="grounded",
            grounded=True,
            sources=["https://codes.example.gov/x"],
            accepted_sources=["https://codes.example.gov/x"],
        )
        fp_markham = ProjectProfile("Markham", "ON", "CA", "X").jurisdiction_fingerprint()
        fp_ashburn = ProjectProfile("Ashburn", "VA", "US", "X").jurisdiction_fingerprint()
        cache.put(
            finding, cycle=DEFAULT_CYCLE, result=result,
            jurisdiction_fingerprint=fp_markham,
        )
        # Same city replays; a different city — and the profile-less key —
        # never see the Markham verdict.
        assert cache.get(
            finding, cycle=DEFAULT_CYCLE, jurisdiction_fingerprint=fp_markham
        ) is not None
        assert cache.get(
            finding, cycle=DEFAULT_CYCLE, jurisdiction_fingerprint=fp_ashburn
        ) is None
        assert cache.get(finding, cycle=DEFAULT_CYCLE) is None

    def test_profile_less_entries_stay_warm(self):
        cache = VerificationCache()
        finding = _finding()
        result = VerificationResult(
            verdict="CONFIRMED",
            explanation="grounded",
            grounded=True,
            sources=["https://codes.example.gov/x"],
            accepted_sources=["https://codes.example.gov/x"],
        )
        cache.put(finding, cycle=DEFAULT_CYCLE, result=result)
        assert cache.get(finding, cycle=DEFAULT_CYCLE) is not None


# ---------------------------------------------------------------------------
# Sidecar v4 + profile.json
# ---------------------------------------------------------------------------


def _lc_finding() -> Finding:
    return Finding(
        severity="HIGH",
        fileName="21 13 13 Wet-Pipe.docx",
        section="[Compliance] 1.2",
        issue="Missing municipal amendment (r-bbbbbbbbbbbb).",
        actionType="ADD",
        existingText=None,
        replacementText="Comply with Municipal Amendment 12-2024.",
        codeReference="Municipal Amendment 12-2024",
        confidence=0.85,
        anchorText="PART 1 - GENERAL",
        insertPosition="after",
        finding_id="lc-0123456789ab",
    )


def _pipeline_result(*, with_profile: bool):
    from src.orchestration.pipeline import PipelineResult

    coverage = [
        {"requirement_id": "r-bbbbbbbbbbbb", "status": "missing",
         "evidence": None, "fileName": None},
    ]
    compliance = ReviewResult(
        findings=[_lc_finding()], cross_check_status="completed", coverage=coverage
    )
    profile_dict = {
        "items": [
            {
                "item_id": "r-bbbbbbbbbbbb",
                "dimension_id": "governing_codes",
                "topic": "Municipal amendment",
                "category": "local_amendment",
                "requirement": "Municipal Amendment 12-2024 applies.",
                "grounded": True,
                "accepted_sources": ["https://city.example.gov/amendment"],
                "confidence": 0.9,
                "actionability": "spec_requirement",
            }
        ],
        "dimension_statuses": [
            {"dimension_id": "governing_codes", "status": "completed", "item_count": 1}
        ],
        "research_date": "2026-07-14",
        "project": _profile().to_dict(),
    }
    return PipelineResult(
        review_result=ReviewResult(findings=[]),
        files_reviewed=["21 13 13 Wet-Pipe.docx"],
        project_profile=_profile().to_dict() if with_profile else None,
        requirements_profile=profile_dict if with_profile else None,
        compliance_result=compliance if with_profile else None,
    )


class TestSidecarV4:
    def test_v4_shape_includes_compliance_findings_and_project(self, tmp_path):
        from src.output.edit_sidecar import (
            SIDECAR_SCHEMA_VERSION,
            write_edit_instructions_sidecar,
        )

        assert SIDECAR_SCHEMA_VERSION == 4
        report = tmp_path / "report.docx"
        sidecar = write_edit_instructions_sidecar(
            _pipeline_result(with_profile=True), report
        )
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        assert data["schema_version"] == 4
        assert data["project"] == _profile().to_dict()
        assert data["requirements_coverage"][0]["requirement_id"] == "r-bbbbbbbbbbbb"
        assert data["edit_count"] == 1
        entry = data["edits"][0]
        assert entry["finding_id"] == "lc-0123456789ab"
        assert entry["edit_proposal"]["action_type"] == "ADD"

    def test_profile_less_sidecar_has_empty_ws4_keys(self, tmp_path):
        from src.output.edit_sidecar import write_edit_instructions_sidecar

        report = tmp_path / "report.docx"
        sidecar = write_edit_instructions_sidecar(
            _pipeline_result(with_profile=False), report
        )
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        assert data["project"] is None
        assert data["requirements_coverage"] == []

    def test_profile_json_written_and_round_trips(self, tmp_path):
        from src.output.edit_sidecar import write_requirements_profile_sidecar
        from src.research import RequirementsProfile

        report = tmp_path / "report.docx"
        path = write_requirements_profile_sidecar(
            _pipeline_result(with_profile=True), report
        )
        assert path is not None and path.name == "report.profile.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["research_date"] == "2026-07-14"
        assert data["project"] == _profile().to_dict()
        assert data["requirements_coverage"][0]["status"] == "missing"
        assert data["compliance_status"] == "completed"
        assert RequirementsProfile.from_dict(data["requirements_profile"]) is not None

    def test_profile_json_skipped_without_profile(self, tmp_path):
        from src.output.edit_sidecar import write_requirements_profile_sidecar

        report = tmp_path / "report.docx"
        assert (
            write_requirements_profile_sidecar(
                _pipeline_result(with_profile=False), report
            )
            is None
        )
        assert not (tmp_path / "report.profile.json").exists()
