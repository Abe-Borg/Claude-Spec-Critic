"""Pins for the immutable governing basis (plan step 2, sections 5.3 / 5.4).

The defect this contract exists to close is an **inversion**: research
establishes which editions a jurisdiction actually adopted, that reaches the
review prompt, and then verification — which never sees it, and is told to
treat the module's ``UNVERIFIED`` pins as authoritative — can overrule a
correct adoption-deferring finding as ``DISPUTED``. A false positive gets human
review. A true positive discarded as DISPUTED does not.

Every test below guards a way the snapshot could quietly stop meaning what it
claims:

* meaning preserved verbatim, structure normalized;
* grounding re-derived rather than trusted, and research citations kept
  strictly out of the verifier's current-conversation retrieval pool;
* contradictions kept, not resolved by model confidence;
* contractual obligations kept apart from adopted law;
* selection deterministic, whole-item, and honest about what it dropped;
* identity derived, never supplied, and never silently reinterpreted.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

import pytest

from src.modules.registry import AVAILABLE_MODULES, get_module
from src.review.structured_schemas import (
    RESEARCH_ACTIONABILITY_VALUES,
    RESEARCH_ITEM_CATEGORIES,
)
from src.verification.governing_context import (
    AUTHORITY_ADOPTED_LAW,
    AUTHORITY_CONTRACTUAL,
    AUTHORITY_OTHER,
    BASIS_POLICY_VERSION,
    BASIS_SCHEMA_VERSION,
    DEFAULT_BASIS_TOKEN_BUDGET,
    MODE_PROVENANCE_ONLY,
    MODE_RESEARCHED_CONTEXT,
    RESEARCH_STATE_AVAILABLE,
    RESEARCH_STATE_PARTIAL,
    RESEARCH_STATE_RECOVERED,
    RESEARCH_STATE_UNAVAILABLE,
    BasisPolicyIncompatible,
    basis_from_dict,
    build_verification_basis,
    historical_source_urls,
    module_basis_from_cycle,
    recovered_basis,
    render_basis_text,
    validate_basis,
)


# ---------------------------------------------------------------------------
# Structural doubles.
#
# Deliberately NOT the real research dataclasses: this module must stay usable
# without importing ``src/research/``, which imports ``src/verification/`` and
# would close an import cycle. TestStructuralContract below pins the doubles
# against the real field names so the doubles cannot drift into fiction.
# ---------------------------------------------------------------------------


@dataclass
class FakeItem:
    item_id: str = "r-1"
    dimension_id: str = "adoption"
    topic: str = "Sprinkler standard edition"
    category: str = "governing_code"
    requirement: str = "NFPA 13-2019 applies."
    authority: str = "Virginia USBC"
    code_reference: str = "13VAC5-63"
    source_urls: list = field(default_factory=list)
    accepted_sources: list = field(default_factory=lambda: ["https://law.example/usbc"])
    grounded: bool = True
    confidence: float = 0.9
    actionability: str = "spec_requirement"
    notes: str = ""


@dataclass
class FakeStatus:
    dimension_id: str = "adoption"
    status: str = "completed"
    item_count: int = 1
    error: str = ""


@dataclass
class FakeProfile:
    items: list = field(default_factory=list)
    dimension_statuses: list = field(default_factory=list)
    research_date: str = "2026-09-01"
    project: dict | None = None


def _dc_cycle():
    module = get_module("datacenter_fire")
    return module.module_id, module.cycle


def _basis(items=None, statuses=None, **kw):
    module_id, cycle = _dc_cycle()
    profile = FakeProfile(
        items=list(items or [FakeItem()]),
        dimension_statuses=list(statuses if statuses is not None else [FakeStatus()]),
        project={"city": "Ashburn", "state_or_province": "VA", "country": "US"},
    )
    return build_verification_basis(
        module_id=module_id, cycle=cycle, profile=profile, **kw
    )


class TestStructuralContract:
    """The doubles above must describe the real research types.

    ``src/research/`` imports ``src/verification/``, so the basis cannot import
    the research runner back and is built against attribute names instead. That
    is only safe while the names are real — otherwise these tests would pass
    against a shape production never produces.
    """

    def test_fake_item_matches_the_real_research_item_fields(self):
        from src.research.requirements_research import ResearchItem

        real = {f for f in ResearchItem.__dataclass_fields__}
        assert set(FakeItem.__dataclass_fields__) == real

    def test_fake_profile_fields_exist_on_the_real_profile(self):
        from src.research.requirements_research import RequirementsProfile

        real = set(RequirementsProfile.__dataclass_fields__)
        assert set(FakeProfile.__dataclass_fields__) <= real

    def test_fake_status_fields_exist_on_the_real_status(self):
        from src.research.requirements_research import DimensionStatus

        real = set(DimensionStatus.__dataclass_fields__)
        assert set(FakeStatus.__dataclass_fields__) <= real

    def test_verification_still_does_not_import_research(self):
        """The cycle this contract's design exists to avoid."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "verification"
        offenders = [
            p.name
            for p in root.rglob("*.py")
            if "from ..research" in p.read_text(encoding="utf-8")
            or "from src.research" in p.read_text(encoding="utf-8")
        ]
        assert offenders == []


class TestVerbatimMeaning:
    """Rule 1: structure is normalized, legal meaning is not."""

    def test_requirement_and_qualifications_survive_byte_for_byte(self):
        requirement = (
            "Sprinkler systems shall comply with NFPA 13-2019 as adopted, "
            "except where §903.3.1.1 is amended locally."
        )
        notes = "Applies only to buildings over 75 ft; retrofits excluded."
        basis = _basis([FakeItem(requirement=requirement, notes=notes)])
        item = basis.items[0]
        assert item.requirement == requirement
        assert item.notes == notes
        assert requirement in render_basis_text(basis)
        assert notes in render_basis_text(basis)

    def test_authority_and_code_reference_are_not_reworded(self):
        basis = _basis(
            [FakeItem(authority="Ohio Board of Building Standards", code_reference="OBC 903.2.9")]
        )
        assert basis.items[0].authority == "Ohio Board of Building Standards"
        assert basis.items[0].code_reference == "OBC 903.2.9"

    def test_a_qualification_is_never_cut_mid_sentence(self):
        """Rule 8: whole items are dropped; a half-caveat is a different rule."""
        long_note = "Exempt where " + ("x" * 4000) + " and only then."
        basis = _basis(
            [FakeItem(item_id="r-1", notes=long_note), FakeItem(item_id="r-2")],
            token_budget=200,
        )
        for item in basis.items:
            if item.item_id == "r-1":
                assert item.notes == long_note


class TestGroundingIsReDerived:
    """Rule 2. A claim of grounding without a citation is not grounding."""

    def test_claimed_grounding_without_a_citation_is_recorded_as_ungrounded(self):
        basis = _basis([FakeItem(grounded=True, accepted_sources=[])])
        item = basis.items[0]
        assert item.grounded is False
        assert item.consistency_note

    def test_the_inconsistency_is_disclosed_in_the_rendered_block(self):
        basis = _basis([FakeItem(grounded=True, accepted_sources=[])])
        text = render_basis_text(basis)
        assert "NOT grounded by research" in text

    def test_grounding_with_a_citation_is_kept(self):
        basis = _basis([FakeItem(grounded=True, accepted_sources=["https://a.example"])])
        assert basis.items[0].grounded is True

    def test_an_ungrounded_claim_is_never_promoted(self):
        basis = _basis([FakeItem(grounded=False, accepted_sources=["https://a.example"])])
        assert basis.items[0].grounded is False

    def test_only_grounded_non_advisory_adoption_facts_are_controlling(self):
        basis = _basis(
            [
                FakeItem(item_id="r-grounded"),
                FakeItem(item_id="r-ungrounded", grounded=False),
                FakeItem(item_id="r-advisory", actionability="process_advisory"),
                FakeItem(item_id="r-client", category="client_standard"),
            ]
        )
        assert {i.item_id for i in basis.controlling_items} == {"r-grounded"}


class TestHistoricalCitationsStayHistorical:
    """Rule 2, second half — the part that protects the grounding invariant.

    ``CONFIRMED``/``CORRECTED``/``DISPUTED`` require a URL retrieved in *this*
    conversation. A research URL is provenance for a claim made days earlier;
    letting it into the accepted pool would let a verdict ground itself on
    evidence the verifier never retrieved.
    """

    def test_research_urls_are_exposed_under_a_historical_name(self):
        basis = _basis(
            [
                FakeItem(item_id="r-1", accepted_sources=["https://a.example/1"]),
                FakeItem(item_id="r-2", accepted_sources=["https://b.example/2"]),
            ]
        )
        assert historical_source_urls(basis) == (
            "https://a.example/1",
            "https://b.example/2",
        )

    def test_duplicate_urls_collapse_in_declaration_order(self):
        basis = _basis(
            [
                FakeItem(item_id="r-1", accepted_sources=["https://a.example", "https://b.example"]),
                FakeItem(item_id="r-2", accepted_sources=["https://a.example"]),
            ]
        )
        assert historical_source_urls(basis) == ("https://a.example", "https://b.example")

    def test_the_rendered_block_says_these_are_not_this_conversations_sources(self):
        text = render_basis_text(_basis())
        assert "do NOT count as sources retrieved in this conversation" in text

    def test_the_basis_exposes_no_field_named_like_accepted_evidence(self):
        """A field named ``sources`` would invite exactly the wrong plumbing."""
        item = _basis().items[0]
        assert not hasattr(item, "sources")
        assert not hasattr(item, "accepted_sources")


class TestContradictionsAreKept:
    """Rule 5. Picking a winner by confidence manufactures certainty."""

    def test_both_sides_of_a_contradiction_survive(self):
        basis = _basis(
            [
                FakeItem(item_id="r-a", requirement="NFPA 13-2019 applies.", confidence=0.9),
                FakeItem(item_id="r-b", requirement="NFPA 13-2022 applies.", confidence=0.4),
            ]
        )
        assert {i.item_id for i in basis.items} == {"r-a", "r-b"}
        text = render_basis_text(basis)
        assert "NFPA 13-2019 applies." in text
        assert "NFPA 13-2022 applies." in text

    def test_a_lower_confidence_claim_is_not_dropped_for_disagreeing(self):
        basis = _basis(
            [
                FakeItem(item_id="r-a", confidence=0.99),
                FakeItem(item_id="r-b", requirement="Contradicts r-a.", confidence=0.01),
            ]
        )
        assert len(basis.items) == 2


class TestAuthoritySeparation:
    """Rule 4. A contractual obligation is not a legal adoption."""

    @pytest.mark.parametrize(
        "category",
        ["governing_code", "local_amendment", "referenced_standard", "ahj_requirement"],
    )
    def test_adoption_categories_carry_adopted_law_authority(self, category):
        basis = _basis([FakeItem(category=category)])
        assert basis.items[0].authority_class == AUTHORITY_ADOPTED_LAW

    @pytest.mark.parametrize("category", ["client_standard", "insurer_requirement"])
    def test_contractual_categories_are_labelled_separately(self, category):
        basis = _basis([FakeItem(category=category)])
        assert basis.items[0].authority_class == AUTHORITY_CONTRACTUAL

    def test_a_contractual_requirement_is_never_controlling_for_adoption(self):
        basis = _basis([FakeItem(category="insurer_requirement")])
        assert basis.controlling_items == ()

    def test_the_rendering_states_why_the_split_matters(self):
        text = render_basis_text(_basis([FakeItem(category="client_standard")]))
        assert "CONTRACTUAL authority, not adopted law" in text
        assert "stricter than code without the code citation being wrong" in text

    def test_every_closed_vocabulary_category_maps_to_a_known_authority(self):
        for category in RESEARCH_ITEM_CATEGORIES:
            basis = _basis([FakeItem(category=category)])
            assert basis.items[0].authority_class in {
                AUTHORITY_ADOPTED_LAW,
                AUTHORITY_CONTRACTUAL,
                AUTHORITY_OTHER,
            }


class TestAdvisoriesStayAdvisory:
    """Rule 3, mirroring the compliance pass: advisories never control."""

    def test_a_process_advisory_keeps_its_classification(self):
        basis = _basis([FakeItem(actionability="process_advisory")])
        assert basis.items[0].is_advisory is True
        assert basis.items[0].actionability == "process_advisory"

    def test_an_advisory_is_excluded_from_controlling_items(self):
        basis = _basis([FakeItem(actionability="process_advisory")])
        assert basis.controlling_items == ()

    def test_an_advisory_is_marked_as_such_in_the_rendered_block(self):
        text = render_basis_text(_basis([FakeItem(actionability="process_advisory")]))
        assert "not a specification requirement" in text

    def test_an_unknown_actionability_is_coerced_to_the_checkable_side(self):
        """Over-checking is safe; silently skipping a requirement is not."""
        basis = _basis([FakeItem(actionability="not-a-real-value")])
        assert basis.items[0].actionability == "spec_requirement"
        assert basis.items[0].actionability in RESEARCH_ACTIONABILITY_VALUES


class TestDeterministicBoundedSelection:
    """Rule 8: same facts, same subset — and every omission stated."""

    def test_selection_is_stable_across_input_order(self):
        items = [
            FakeItem(item_id=f"r-{i}", category=c, notes="n" * 500)
            for i, c in enumerate(
                ["client_standard", "governing_code", "site_environment", "ahj_requirement"]
            )
        ]
        a = _basis(list(items), token_budget=400)
        b = _basis(list(reversed(items)), token_budget=400)
        assert [i.item_id for i in a.items] == [i.item_id for i in b.items]
        assert a.fingerprint() == b.fingerprint()

    def test_adoption_facts_outrank_contractual_and_site_context(self):
        basis = _basis(
            [
                FakeItem(item_id="r-site", category="site_environment", notes="n" * 900),
                FakeItem(item_id="r-client", category="client_standard", notes="n" * 900),
                FakeItem(item_id="r-code", category="governing_code", notes="n" * 900),
            ],
            token_budget=320,
        )
        assert basis.items[0].item_id == "r-code"

    def test_high_confidence_site_context_cannot_displace_a_grounded_adoption_fact(self):
        basis = _basis(
            [
                FakeItem(item_id="r-site", category="site_environment", confidence=1.0, notes="n" * 900),
                FakeItem(item_id="r-code", category="governing_code", confidence=0.1, notes="n" * 900),
            ],
            token_budget=290,
        )
        assert [i.item_id for i in basis.items] == ["r-code"]

    def test_dropped_items_are_named_in_omissions_not_silently_lost(self):
        basis = _basis(
            [FakeItem(item_id=f"r-{i}", notes="n" * 900) for i in range(6)],
            token_budget=300,
        )
        assert len(basis.items) < 6
        joined = " ".join(basis.omissions)
        assert "omitted to stay within" in joined
        assert "NOT settled by this basis" in joined

    def test_omissions_are_rendered_into_the_prompt_text(self):
        basis = _basis(
            [FakeItem(item_id=f"r-{i}", notes="n" * 900) for i in range(6)],
            token_budget=300,
        )
        text = render_basis_text(basis)
        assert "Known limits of this basis:" in text
        for note in basis.omissions:
            assert note in text

    def test_a_single_oversized_item_is_kept_whole_and_the_breach_disclosed(self):
        basis = _basis([FakeItem(notes="n" * 8000)], token_budget=100)
        assert len(basis.items) == 1
        assert any("over the 100-token basis budget" in o for o in basis.omissions)

    def test_the_size_estimate_accounts_for_render_scaffolding(self):
        """A budget that counts less than it renders is not a budget."""
        from src.verification.governing_context import _item_size_estimate, _render_item

        item = _basis([FakeItem(notes="n" * 300)]).items[0]
        rendered = "\n".join(_render_item(item))
        assert _item_size_estimate(item) >= len(rendered) / 4.5

    def test_the_default_budget_is_the_documented_value(self):
        assert DEFAULT_BASIS_TOKEN_BUDGET == 4_000


class TestResearchState:
    """A partial fan-out must not read as a complete one."""

    def test_all_dimensions_completed_is_available(self):
        basis = _basis(statuses=[FakeStatus(), FakeStatus(dimension_id="amendments")])
        assert basis.research_state == RESEARCH_STATE_AVAILABLE

    def test_one_failed_dimension_is_partial(self):
        basis = _basis(
            statuses=[
                FakeStatus(),
                FakeStatus(dimension_id="amendments", status="failed", error="429"),
            ]
        )
        assert basis.research_state == RESEARCH_STATE_PARTIAL

    def test_all_failed_is_unavailable(self):
        basis = _basis(statuses=[FakeStatus(status="failed", error="429")])
        assert basis.research_state == RESEARCH_STATE_UNAVAILABLE

    def test_a_failed_dimension_names_what_is_missing_not_absent(self):
        basis = _basis(
            statuses=[
                FakeStatus(),
                FakeStatus(dimension_id="amendments", status="failed", error="429 rate limit"),
            ]
        )
        joined = " ".join(basis.omissions)
        assert "'amendments' did not complete" in joined
        assert "429 rate limit" in joined
        assert "missing, not absent" in joined

    def test_the_research_state_reaches_the_rendered_block(self):
        basis = _basis(statuses=[FakeStatus(status="failed")])
        assert f"Research state: {RESEARCH_STATE_UNAVAILABLE}" in render_basis_text(basis)

    def test_the_research_date_is_disclosed(self):
        assert "Research as of: 2026-09-01" in render_basis_text(_basis())


class TestProvenanceOnlyMode:
    """A profile-less run must disclose assumptions, not inherit authority."""

    def test_no_profile_yields_provenance_only(self):
        module_id, cycle = _dc_cycle()
        basis = build_verification_basis(module_id=module_id, cycle=cycle, profile=None)
        assert basis.mode == MODE_PROVENANCE_ONLY
        assert basis.items == ()
        assert basis.research_state == RESEARCH_STATE_UNAVAILABLE

    def test_it_states_the_pins_are_not_established_adoptions(self):
        module_id, cycle = _dc_cycle()
        basis = build_verification_basis(module_id=module_id, cycle=cycle, profile=None)
        joined = " ".join(basis.omissions)
        assert "No jurisdiction research is attached" in joined
        assert "not established adoptions for this project" in joined

    def test_unverified_pins_are_disclosed_in_provenance_only_mode_too(self):
        """The mode where the pins are the *only* thing carried.

        Emitting this disclosure solely on the researched path would drop it
        exactly where nothing else compensates for its absence.
        """
        module_id, cycle = _dc_cycle()
        basis = build_verification_basis(module_id=module_id, cycle=cycle, profile=None)
        assert any("marked UNVERIFIED" in o for o in basis.omissions)

    def test_unverified_pins_are_disclosed_on_the_researched_path(self):
        assert any("marked UNVERIFIED" in o for o in _basis().omissions)

    def test_pin_provenance_survives_into_the_rendered_block(self):
        module_id, cycle = _dc_cycle()
        text = render_basis_text(
            build_verification_basis(module_id=module_id, cycle=cycle, profile=None)
        )
        assert "[UNVERIFIED provenance]" in text
        assert "module reference assumption" in text


class TestRecoveredBasis:
    """Resume must ask the question it paid for, or say it cannot."""

    def test_recovered_is_distinct_from_unavailable(self):
        module_id, cycle = _dc_cycle()
        rec = recovered_basis(module_id, cycle)
        assert rec.research_state == RESEARCH_STATE_RECOVERED
        assert rec.research_state != RESEARCH_STATE_UNAVAILABLE

    def test_it_refuses_to_present_todays_pins_as_the_originals(self):
        module_id, cycle = _dc_cycle()
        joined = " ".join(recovered_basis(module_id, cycle).omissions)
        assert "cannot be reconstructed" in joined
        assert "TODAY's values" in joined

    def test_a_recovered_basis_has_a_different_identity_from_a_fresh_one(self):
        module_id, cycle = _dc_cycle()
        fresh = build_verification_basis(module_id=module_id, cycle=cycle, profile=None)
        assert recovered_basis(module_id, cycle).fingerprint() != fresh.fingerprint()


class TestFingerprintIdentity:
    """Identity is derived. A caller-supplied one could be made to collide."""

    def test_the_same_inputs_agree(self):
        assert _basis().fingerprint() == _basis().fingerprint()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"requirement": "NFPA 13-2022 applies."},
            {"notes": "Only above 75 ft."},
            {"code_reference": "13VAC5-63 §101"},
            {"authority": "Loudoun County"},
            {"category": "client_standard"},
            {"actionability": "process_advisory"},
            {"grounded": False},
            {"item_id": "r-other"},
        ],
    )
    def test_a_materially_different_fact_changes_the_identity(self, kwargs):
        assert _basis([FakeItem(**kwargs)]).fingerprint() != _basis().fingerprint()

    def test_a_different_research_state_changes_the_identity(self):
        a = _basis(statuses=[FakeStatus()])
        b = _basis(statuses=[FakeStatus(status="failed", error="429")])
        assert a.fingerprint() != b.fingerprint()

    def test_a_different_location_changes_the_identity(self):
        module_id, cycle = _dc_cycle()
        va = build_verification_basis(
            module_id=module_id,
            cycle=cycle,
            profile=FakeProfile(items=[FakeItem()], dimension_statuses=[FakeStatus()]),
            project={"city": "Ashburn", "state_or_province": "VA"},
        )
        oh = build_verification_basis(
            module_id=module_id,
            cycle=cycle,
            profile=FakeProfile(items=[FakeItem()], dimension_statuses=[FakeStatus()]),
            project={"city": "New Albany", "state_or_province": "OH"},
        )
        assert va.fingerprint() != oh.fingerprint()

    def test_dropping_an_item_changes_the_identity(self):
        """A basis that omitted a fact is a different question, not the same one."""
        items = [FakeItem(item_id=f"r-{i}", notes="n" * 900) for i in range(6)]
        full = _basis(list(items), token_budget=100_000)
        trimmed = _basis(list(items), token_budget=300)
        assert full.fingerprint() != trimmed.fingerprint()

    def test_omissions_alone_change_the_identity(self):
        """The case the item list cannot prove.

        ``test_dropping_an_item_changes_the_identity`` passes even if omissions
        were left out of the fingerprint entirely, because dropping an item
        also changes the item list. Here the items, statuses and module basis
        are identical and *only* the disclosed limits differ — one basis
        discloses that it breached its budget, the other does not.
        """
        over = _basis([FakeItem(notes="n" * 8000)], token_budget=100)
        under = _basis([FakeItem(notes="n" * 8000)], token_budget=100_000)
        assert over.items == under.items
        assert over.dimension_statuses == under.dimension_statuses
        assert over.omissions != under.omissions
        assert over.fingerprint() != under.fingerprint()

    def test_two_bases_that_render_differently_never_share_an_identity(self):
        """The completeness property the individual cases approximate.

        The fingerprint keys a verification cache. If two bases can render
        different prompts under one identity, a verdict grounded against one
        set of assumptions replays for a run that was told something else —
        which is the reuse the key exists to prevent.
        """
        variants = [
            _basis(),
            _basis([FakeItem(requirement="NFPA 13-2022 applies.")]),
            _basis([FakeItem(notes="Only above 75 ft.")]),
            _basis([FakeItem(category="client_standard")]),
            _basis([FakeItem(actionability="process_advisory")]),
            _basis([FakeItem(grounded=True, accepted_sources=[])]),
            _basis(statuses=[FakeStatus(status="failed", error="429")]),
            _basis([FakeItem(notes="n" * 8000)], token_budget=100),
            _basis([FakeItem(notes="n" * 8000)], token_budget=100_000),
            _basis([FakeItem(item_id=f"r-{i}") for i in range(3)]),
        ]
        by_fingerprint: dict[str, str] = {}
        for basis in variants:
            text = render_basis_text(basis)
            fp = basis.fingerprint()
            if fp in by_fingerprint:
                assert by_fingerprint[fp] == text, (
                    "two bases render different prompts under one fingerprint"
                )
            by_fingerprint[fp] = text
        assert len(by_fingerprint) == len(variants)

    def test_confidence_alone_does_not_change_the_identity(self):
        """Confidence is not part of the question being asked.

        Two runs that established the same facts must share a cache identity
        even if the research model rated its own certainty differently.
        """
        a = _basis([FakeItem(confidence=0.9)])
        b = _basis([FakeItem(confidence=0.4)])
        assert a.fingerprint() == b.fingerprint()

    def test_the_fingerprint_cannot_be_supplied_by_a_caller(self):
        basis = _basis()
        stored = basis.to_dict()
        stored["fingerprint"] = "0" * 24
        assert basis_from_dict(stored).fingerprint() == basis.fingerprint()

    def test_the_fingerprint_is_a_fixed_width_hex_digest(self):
        fp = _basis().fingerprint()
        assert len(fp) == 24
        assert all(c in "0123456789abcdef" for c in fp)


class TestSerializationRoundTrip:
    def test_round_trip_preserves_identity_and_content(self):
        basis = _basis(
            [
                FakeItem(item_id="r-1", notes="Only above 75 ft."),
                FakeItem(item_id="r-2", category="insurer_requirement", grounded=False),
            ]
        )
        restored = basis_from_dict(json.loads(json.dumps(basis.to_dict())))
        assert restored.fingerprint() == basis.fingerprint()
        assert restored.items == basis.items
        assert restored.omissions == basis.omissions
        assert restored.module_basis == basis.module_basis

    def test_a_round_tripped_basis_renders_identically(self):
        basis = _basis()
        restored = basis_from_dict(json.loads(json.dumps(basis.to_dict())))
        assert render_basis_text(restored) == render_basis_text(basis)

    def test_an_unknown_schema_version_is_refused_not_reinterpreted(self):
        stored = _basis().to_dict()
        stored["schema_version"] = BASIS_SCHEMA_VERSION + 1
        with pytest.raises(BasisPolicyIncompatible):
            basis_from_dict(stored)

    def test_an_unknown_policy_version_is_refused_not_reinterpreted(self):
        """Re-reading an old snapshot under today's authority rules would
        change the question a paid run actually asked."""
        stored = _basis().to_dict()
        stored["policy_version"] = BASIS_POLICY_VERSION + 1
        with pytest.raises(BasisPolicyIncompatible) as exc:
            basis_from_dict(stored)
        assert "authority policy" in str(exc.value)

    def test_the_two_versions_are_independent_knobs(self):
        """A shape change and a semantics change are not the same event."""
        import src.verification.governing_context as gc
        import inspect

        src = inspect.getsource(gc)
        assert "BASIS_SCHEMA_VERSION = " in src
        assert "BASIS_POLICY_VERSION = " in src


class TestValidation:
    def test_a_well_formed_basis_has_no_problems(self):
        assert validate_basis(_basis()) == []

    def test_provenance_only_with_items_is_a_problem(self):
        bad = replace(_basis(), mode=MODE_PROVENANCE_ONLY)
        assert any("must carry no researched items" in p for p in validate_basis(bad))

    def test_grounded_without_sources_is_caught_defensively(self):
        basis = _basis()
        bad = replace(basis, items=(replace(basis.items[0], historical_sources=()),))
        assert any("grounded with no historical sources" in p for p in validate_basis(bad))

    def test_an_unknown_research_state_is_caught(self):
        bad = replace(_basis(), research_state="probably_fine")
        assert any("research_state" in p for p in validate_basis(bad))


class TestModuleBasisSnapshot:
    """A cycle label alone cannot reconstruct the assumptions later."""

    def test_it_captures_editions_not_just_the_label(self):
        module_id, cycle = _dc_cycle()
        mb = module_basis_from_cycle(module_id, cycle)
        assert mb.cycle_label == "dc-ibc-2024"
        assert mb.primary_code_year == "2024"
        assert mb.asce7 == "7-22"
        assert any(s.name == "NFPA 13" for s in mb.standards)

    def test_unverified_provenance_is_derived_from_the_source_marker(self):
        module_id, cycle = _dc_cycle()
        mb = module_basis_from_cycle(module_id, cycle)
        assert mb.has_unverified_pins
        for pin in mb.standards:
            assert pin.unverified == pin.provenance.strip().upper().startswith("UNVERIFIED")

    @pytest.mark.parametrize("module_id", sorted(AVAILABLE_MODULES))
    def test_every_registered_module_snapshots_without_error(self, module_id):
        module = AVAILABLE_MODULES[module_id]
        basis = build_verification_basis(
            module_id=module_id, cycle=module.cycle, profile=None
        )
        assert validate_basis(basis) == []
        assert render_basis_text(basis).strip()

    def test_the_california_module_pins_are_not_all_unverified(self):
        """Guards the derivation: a flag that is always True proves nothing."""
        module = get_module("california_k12_mep")
        mb = module_basis_from_cycle(module.module_id, module.cycle)
        assert any(not s.unverified for s in mb.standards)


class TestRenderingFraming:
    """What the verifier is told this block is — and is not."""

    def test_it_is_framed_as_context_not_evidence(self):
        text = render_basis_text(_basis())
        assert "This is recorded context, not evidence." in text

    def test_researched_claims_are_framed_as_claims_to_investigate(self):
        assert "claims to investigate" in render_basis_text(_basis())

    def test_module_pins_are_framed_as_reference_assumptions(self):
        assert "reference assumptions unless applicability is established" in render_basis_text(_basis())

    def test_the_block_ends_with_exactly_one_newline(self):
        text = render_basis_text(_basis())
        assert text.endswith("\n")
        assert not text.endswith("\n\n")

    def test_rendering_is_deterministic(self):
        assert render_basis_text(_basis()) == render_basis_text(_basis())

    def test_rendering_never_mutates_the_basis(self):
        basis = _basis()
        before = basis.fingerprint()
        render_basis_text(basis)
        assert basis.fingerprint() == before


class TestEmptyAndDegenerateInputs:
    def test_a_profile_with_no_items_is_not_an_error(self):
        module_id, cycle = _dc_cycle()
        basis = build_verification_basis(
            module_id=module_id,
            cycle=cycle,
            profile=FakeProfile(items=[], dimension_statuses=[]),
        )
        assert basis.items == ()
        assert basis.research_state == RESEARCH_STATE_UNAVAILABLE
        assert validate_basis(basis) == []

    def test_a_plain_dict_profile_is_accepted(self):
        """The structural contract, exercised through its other shape."""
        module_id, cycle = _dc_cycle()
        basis = build_verification_basis(
            module_id=module_id,
            cycle=cycle,
            profile={
                "items": [
                    {
                        "item_id": "r-1",
                        "category": "governing_code",
                        "requirement": "NFPA 13-2019 applies.",
                        "grounded": True,
                        "accepted_sources": ["https://a.example"],
                    }
                ],
                "dimension_statuses": [{"dimension_id": "adoption", "status": "completed"}],
                "research_date": "2026-09-01",
            },
        )
        assert basis.mode == MODE_RESEARCHED_CONTEXT
        assert basis.items[0].requirement == "NFPA 13-2019 applies."
        assert basis.items[0].grounded is True

    def test_an_unknown_category_degrades_without_raising(self):
        basis = _basis([FakeItem(category="not-a-category")])
        assert basis.items[0].category in RESEARCH_ITEM_CATEGORIES
        assert basis.items[0].authority_class == AUTHORITY_OTHER

    def test_missing_optional_fields_do_not_raise(self):
        module_id, cycle = _dc_cycle()
        basis = build_verification_basis(
            module_id=module_id, cycle=cycle, profile={"items": [{}]}
        )
        assert len(basis.items) == 1
        assert validate_basis(basis) == []

    def test_none_valued_fields_are_treated_as_absent(self):
        module_id, cycle = _dc_cycle()
        basis = build_verification_basis(
            module_id=module_id,
            cycle=cycle,
            profile={"items": [{"item_id": "r-1", "notes": None, "confidence": None}]},
        )
        assert basis.items[0].notes == ""
        assert basis.items[0].confidence == 0.0
