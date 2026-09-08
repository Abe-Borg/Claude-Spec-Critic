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
            # The cache enforces the v3 source_quote invariant on CONFIRMED.
            source_quote="Section X applies.",
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
            # The cache enforces the v3 source_quote invariant on CONFIRMED.
            source_quote="Section X applies.",
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


# ---------------------------------------------------------------------------
# B-2: the profile-less web_search location is module data, not engine data
# ---------------------------------------------------------------------------


_CALIFORNIA_LOCATION = {
    "type": "approximate",
    "country": "US",
    "region": "California",
}


def _datacenter_modules():
    from src.modules import (
        DATACENTER_ARCHITECTURE,
        DATACENTER_ELECTRICAL,
        DATACENTER_ELECTRONIC_SAFETY_SECURITY,
        DATACENTER_FIRE,
    )

    return (
        DATACENTER_FIRE,
        DATACENTER_ARCHITECTURE,
        DATACENTER_ELECTRICAL,
        DATACENTER_ELECTRONIC_SAFETY_SECURITY,
    )


class TestModuleDefaultSearchLocation:
    def test_engine_builder_emits_no_location_by_default(self):
        from src.core.api_config import build_web_search_tool

        tool = build_web_search_tool()
        assert "user_location" not in tool
        assert list(tool.keys()) == ["type", "name", "blocked_domains", "max_uses"]

    def test_engine_builder_copies_a_supplied_location(self):
        from src.core.api_config import build_web_search_tool

        loc = dict(_MARKHAM_LOCATION)
        tool = build_web_search_tool(user_location=loc)
        assert tool["user_location"] == _MARKHAM_LOCATION
        tool["user_location"]["city"] = "Elsewhere"
        assert loc["city"] == "Markham"

    def test_california_module_supplies_exactly_the_legacy_dict(self):
        from src.modules import CALIFORNIA_K12_MEP

        assert CALIFORNIA_K12_MEP.default_web_search_user_location == _CALIFORNIA_LOCATION

    def test_profile_less_california_run_is_byte_identical_including_key_order(self):
        decision = select_routing(_finding(), cycle=DEFAULT_CYCLE)
        assert decision.module_id == "california_k12_mep"
        web_search = build_verification_tools_from_decision(decision)[0]
        assert web_search["user_location"] == _CALIFORNIA_LOCATION
        # The former engine default appended the key last; the module default
        # lands in the same slot, so the serialized tool dict is unchanged.
        assert list(web_search.keys()) == [
            "type", "name", "blocked_domains", "max_uses", "user_location",
        ]

    def test_cycle_less_routing_degrades_to_california(self):
        decision = select_routing(_finding())
        assert decision.module_id == "california_k12_mep"
        assert build_verification_tools_from_decision(decision)[0]["user_location"] == (
            _CALIFORNIA_LOCATION
        )

    @pytest.mark.parametrize("module", _datacenter_modules(), ids=lambda m: m.module_id)
    def test_profile_less_datacenter_run_searches_unlocalized(self, module):
        assert module.default_web_search_user_location is None
        decision = select_routing(_finding(), cycle=module.cycle)
        assert decision.module_id == module.module_id
        tools = build_verification_tools_from_decision(decision)
        assert "user_location" not in tools[0]
        request = build_verification_request(
            decision, prompt="verify this", system_prompt="you verify"
        )
        assert "user_location" not in request.params["tools"][0]

    def test_profile_wins_over_the_california_default(self):
        decision = select_routing(_finding(), cycle=DEFAULT_CYCLE)
        tools = build_verification_tools_from_decision(decision, user_location=_MARKHAM_LOCATION)
        assert tools[0]["user_location"] == _MARKHAM_LOCATION

    def test_profile_wins_for_a_datacenter_run(self):
        from src.modules import DATACENTER_FIRE

        decision = select_routing(_finding(), cycle=DATACENTER_FIRE.cycle)
        request = build_verification_request(
            decision,
            prompt="verify this",
            system_prompt="you verify",
            user_location=_MARKHAM_LOCATION,
        )
        assert request.params["tools"][0]["user_location"] == _MARKHAM_LOCATION

    def test_decision_carries_module_id_and_round_trips(self):
        from src.modules import DATACENTER_FIRE
        from src.verification.verification_routing import VerificationRoutingDecision

        decision = select_routing(_finding(), cycle=DATACENTER_FIRE.cycle)
        payload = decision.to_dict()
        assert payload["module_id"] == "datacenter_fire"
        rebuilt = VerificationRoutingDecision.from_dict(payload)
        assert rebuilt == decision
        assert "user_location" not in build_verification_tools_from_decision(rebuilt)[0]

    def test_legacy_decision_row_without_module_id_behaves_like_california(self):
        # A ``request_contexts`` row written before the field existed
        # reconstructs with ``module_id=""`` → the default module → the same
        # California localization the engine used to hardcode.
        from src.verification.verification_routing import VerificationRoutingDecision

        payload = select_routing(_finding(), cycle=DEFAULT_CYCLE).to_dict()
        payload.pop("module_id")
        rebuilt = VerificationRoutingDecision.from_dict(payload)
        assert rebuilt.module_id == ""
        assert build_verification_tools_from_decision(rebuilt)[0]["user_location"] == (
            _CALIFORNIA_LOCATION
        )
