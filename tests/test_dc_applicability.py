"""Pins for the data-center applicability scenario set (plan step 1, section 4.3).

The set is a **specification** for judging the step-2 edition-authority change,
written before any model output is reviewed so the bar cannot be moved to fit a
later result. These tests protect three things:

1. **Coverage** — every dimension plan section 4.3 requires is present, and no
   scenario is missing the parts that make it judgeable.
2. **Non-circularity** — adoption facts are sourced outside this repository.
   An expectation justified by the pins it judges would stay green even if the
   pins were wrong; that mistake was made once already in the oracle ledger.
3. **Contact with the real code** — the scenarios describe a contrast against
   the actual ``datacenter_fire`` pins and the actual review prompt. If either
   moves, the tripwires below fail so the scenarios get revisited instead of
   silently describing a codebase that no longer exists.
"""
from __future__ import annotations

import re
from pathlib import Path

from evals.dc_applicability import (
    EVALUATION_PROTOCOL,
    NOT_APPLICABLE,
    REQUIRED_DIMENSIONS,
    SCENARIOS,
    validate_scenarios,
)

_REPO = Path(__file__).resolve().parents[1]
_DC_FIRE = _REPO / "src" / "modules" / "datacenter_fire.py"


class TestCoverage:
    def test_scenario_set_validates(self):
        assert validate_scenarios() == []

    def test_every_required_dimension_is_covered(self):
        covered = {s.dimension for s in SCENARIOS}
        assert REQUIRED_DIMENSIONS <= covered, sorted(REQUIRED_DIMENSIONS - covered)

    def test_scenario_ids_are_unique(self):
        ids = [s.scenario_id for s in SCENARIOS]
        assert len(ids) == len(set(ids))

    def test_every_scenario_states_what_is_wrong_today(self):
        """Without a stated failure mode there is no bar for step 2 to clear."""
        for s in SCENARIOS:
            assert s.failure_mode_today.strip(), s.scenario_id
            assert s.judging_criteria, s.scenario_id

    def test_every_scenario_names_a_pass_and_a_fail_condition(self):
        for s in SCENARIOS:
            joined = " ".join(s.judging_criteria)
            assert "PASS" in joined, s.scenario_id
            assert "FAIL" in joined, s.scenario_id


class TestNonCircularSourcing:
    """Adoption facts must be grounded outside this repository.

    Citing a pinned edition in ``code_cycles.py`` to justify a scenario that
    judges those pins would be circular: a wrong pin would leave behaviour and
    expectation agreeing, and the scenario would stay green for the wrong reason.
    """

    def test_adoption_facts_cite_external_sources(self):
        for s in SCENARIOS:
            if s.adoption is None:
                continue
            assert s.adoption.sources, s.scenario_id
            for src in s.adoption.sources:
                assert "src/" not in src, (s.scenario_id, src)
                assert "code_cycles" not in src, (s.scenario_id, src)

    def test_researched_scenarios_carry_an_adoption_fact(self):
        for s in SCENARIOS:
            if s.research_state == "available":
                assert s.adoption is not None, s.scenario_id

    def test_unresearched_scenarios_carry_none(self):
        """A scenario with no research must not smuggle in adoption facts."""
        for s in SCENARIOS:
            if s.research_state in {"unavailable", "partial"}:
                assert s.adoption is None, s.scenario_id


class TestTheInversionCase:
    """The section 2.1 inversion is the failure that matters most.

    A false positive (a wrong edition edit) gets human review. A correct
    adoption-deferring finding discarded as DISPUTED does not — that is the
    silent direction, and it must be judged separately so a change cannot
    trade one failure for the other and look like progress.
    """

    def _inversion(self):
        return next(
            s for s in SCENARIOS if s.dimension == "inversion_correct_finding_discarded"
        )

    def test_the_inversion_scenario_exists(self):
        assert self._inversion() is not None

    def test_it_expects_the_correct_finding_to_survive(self):
        s = self._inversion()
        assert s.expected_review_finding.strip()
        assert s.expected_verdict == "CONFIRMED"
        assert s.expected_status == "VERIFIED_SUPPORTED"

    def test_disputed_is_named_as_the_failure(self):
        joined = " ".join(self._inversion().judging_criteria)
        assert "DISPUTED" in joined
        assert re.search(r"FAIL on DISPUTED", joined)

    def test_it_requires_separate_measurement_from_false_positives(self):
        joined = " ".join(self._inversion().judging_criteria).lower()
        assert "separately" in joined


class TestRealCodeTripwires:
    """These fail when the code the scenarios describe changes.

    A failure here does not mean the application is broken — it means a
    scenario now describes something that is no longer true and must be
    rewritten before it is used to judge anything.
    """

    def test_module_still_pins_the_editions_the_scenarios_contrast_against(self):
        src = _DC_FIRE.read_text(encoding="utf-8")
        assert 'label="dc-ibc-2024"' in src
        assert 'BaseCode("ibc", "IBC", "2024"' in src
        assert 'asce7="7-22"' in src
        assert re.search(r'StandardEdition\(\s*"NFPA 13",\s*"2022"', src)

    def test_module_pins_are_still_unverified(self):
        """Every scenario's module_pins block says the pins are UNVERIFIED."""
        src = _DC_FIRE.read_text(encoding="utf-8")
        assert "UNVERIFIED" in src
        for s in SCENARIOS:
            assert "UNVERIFIED" in s.module_pins["provenance"], s.scenario_id

    def test_review_prompt_still_carries_the_asce_example_one_scenario_turns_on(self):
        """``dc_va_asce7_prompt_example_collision`` exists because of this text.

        Category #2 offers 'ASCE 7-16 instead of 7-22' as its example of a
        superseded edition, while the same sentence instructs deference to
        project adoption. In a 2021-IBC jurisdiction ASCE 7-16 is correct, so
        the example and the deference clause point opposite ways on the same
        citation. If this text is rewritten, that scenario needs rewriting too.
        """
        src = _DC_FIRE.read_text(encoding="utf-8")
        assert "ASCE {asce7_prev} instead of {asce7}" in src
        assert "defer to it for edition checks" in src

    def test_verification_still_cannot_see_project_context(self):
        """The precondition the inversion scenario depends on.

        Matched on the **AST**, not on the file text: a module that explains
        why verification cannot see the project context necessarily writes the
        name in prose, and failing on that would make the tripwire fire for
        documentation while telling the reader the code changed.

        Only real access counts — a reference, an attribute, a parameter, a
        keyword argument, or the exact string used as a key. Prose cannot
        produce any of those, and every way the pipeline could actually hand
        the context to a verifier does.

        Step 2 is expected to change this. When it does, this test failing is
        the signal to re-read the inversion scenario's failure_mode_today, not
        a sign that the application regressed.
        """
        import ast

        hits: list[str] = []
        for path in sorted((_REPO / "src" / "verification").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                touched = (
                    (isinstance(node, ast.Name) and node.id == "project_context")
                    or (isinstance(node, ast.Attribute) and node.attr == "project_context")
                    or (isinstance(node, ast.arg) and node.arg == "project_context")
                    or (isinstance(node, ast.keyword) and node.arg == "project_context")
                    or (
                        isinstance(node, ast.Constant)
                        and node.value == "project_context"
                    )
                )
                if touched:
                    hits.append(f"{path.name}:{getattr(node, 'lineno', '?')}")
        assert hits == [], (
            "verification now reads project_context — revisit "
            f"dc_inversion_correct_finding_discarded.failure_mode_today ({hits})"
        )


class TestObservedDetectorBehaviour:
    """Every pre-screen claim is executed, not asserted.

    A criterion about the deterministic detector is worthless if no input in
    the scenario can trigger it — step 2 would "pass" the pre-screen without
    touching it. So each scenario records what the real detector emits for its
    excerpt today, and this class re-runs the detector to keep that honest.
    """

    def _detector(self):
        from src.input.preprocessor import detect_stale_code_cycle_references
        from src.modules.registry import get_module

        cycle = get_module("datacenter_fire").cycle
        return lambda text: tuple(
            a.get("deterministic_rule")
            for a in detect_stale_code_cycle_references(text, "21 13 13 - Sprinklers.docx", cycle)
        )

    def test_recorded_detector_output_matches_the_real_detector(self):
        run = self._detector()
        for s in SCENARIOS:
            assert run(s.spec_excerpt) == s.observed_detector_alerts, (
                f"{s.scenario_id}: recorded observed_detector_alerts is stale. If step 2 "
                "changed the pre-screen, update the record and re-read the scenario's "
                "failure_mode_today and judging_criteria."
            )

    def test_a_detector_criterion_implies_a_detector_that_actually_fires(self):
        """No scenario may claim a pre-screen fix it cannot demonstrate.

        The NFPA-only excerpts this set originally used could never trigger the
        detector at all — the data-center vocabulary scans IBC/IFC/IEBC/IFGC and
        ASCE, never NFPA editions — so the criterion was vacuous.
        """
        for s in SCENARIOS:
            mentions_detector = any(
                "stale-cycle alert" in c or "stale_asce7" in c for c in s.judging_criteria
            )
            if mentions_detector:
                assert s.observed_detector_alerts, (
                    f"{s.scenario_id}: claims a detector outcome but its excerpt "
                    "produces no alerts, so the criterion can never be exercised"
                )

    def test_recorded_pipeline_output_matches_a_real_run(self):
        """The pipeline record is executed too, not just the raw detector.

        These are different measurements and the difference is the point:
        step 2 suppresses stale-cycle detection for a location-aware module,
        so the raw detector can fire while a real run surfaces nothing.
        Recording only the raw output would let a scenario describe an alert
        no run ever shows.
        """
        from src.input.preprocessor import preprocess_spec
        from src.modules.registry import get_module

        cycle = get_module("datacenter_fire").cycle
        for s in SCENARIOS:
            result = preprocess_spec(
                s.spec_excerpt, "21 13 13 - Sprinklers.docx", cycle=cycle
            )
            actual = tuple(
                a.get("deterministic_rule") for a in (result.code_cycle_alerts or [])
            )
            assert actual == s.observed_pipeline_alerts, (
                f"{s.scenario_id}: recorded observed_pipeline_alerts is stale."
            )

    def test_suppression_is_visible_as_a_difference_from_the_raw_detector(self):
        """At least one scenario must show the raw/pipeline split.

        If every scenario had both empty, this pair of records would agree for
        the trivial reason and would stop proving that suppression happens.
        """
        assert any(
            s.observed_detector_alerts and not s.observed_pipeline_alerts
            for s in SCENARIOS
        )

    def test_at_least_one_scenario_exercises_each_detector_rule(self):
        seen = {rule for s in SCENARIOS for rule in s.observed_detector_alerts}
        assert "stale_code_cycle" in seen
        assert "stale_asce7" in seen


class TestFindingExpectationContract:
    """Verification only runs on findings.

    A scenario expecting no finding cannot also expect a verdict: a scorer
    honouring that would fail a correct silent review, and one ignoring it
    would make the comparison ambiguous.
    """

    def test_no_finding_means_no_verdict_expectation(self):
        for s in SCENARIOS:
            if s.finding_expectation == "none":
                assert not s.expected_review_finding.strip(), s.scenario_id
                assert s.expected_verdict.startswith(NOT_APPLICABLE), s.scenario_id
                assert s.expected_status.startswith(NOT_APPLICABLE), s.scenario_id

    def test_required_findings_carry_real_verdicts(self):
        for s in SCENARIOS:
            if s.finding_expectation == "required":
                assert s.expected_review_finding.strip(), s.scenario_id
                assert not s.expected_verdict.startswith(NOT_APPLICABLE), s.scenario_id
                assert not s.expected_status.startswith(NOT_APPLICABLE), s.scenario_id

    def test_optional_findings_say_what_silence_scores_as(self):
        for s in SCENARIOS:
            if s.finding_expectation == "optional":
                joined = " ".join(s.judging_criteria).lower()
                assert "no finding" in joined, s.scenario_id

    def test_every_scenario_declares_an_expectation(self):
        for s in SCENARIOS:
            assert s.finding_expectation in {"none", "optional", "required"}, s.scenario_id


class TestProtocolHonesty:
    """The set must not read as if it had already measured something."""

    def test_protocol_records_that_nothing_has_been_run(self):
        assert "NOT RUN" in EVALUATION_PROTOCOL["status"]

    def test_protocol_requires_authorization_before_spending(self):
        assert "authorization" in EVALUATION_PROTOCOL
        assert "not authorization to spend" in EVALUATION_PROTOCOL["authorization"]

    def test_protocol_states_the_hermetic_limit(self):
        limit = EVALUATION_PROTOCOL["hermetic_limit"]
        assert "do not establish improved model reasoning" in limit

    def test_protocol_requires_both_failure_directions_reported(self):
        paired = EVALUATION_PROTOCOL["paired_comparison"]
        assert "incorrect confirmations" in paired
        assert "incorrect disputes" in paired

    def test_no_scenario_claims_a_measured_result(self):
        """Nothing in the set may assert how a model actually performed."""
        banned = ("we measured", "the model scored", "accuracy was", "% of scenarios")
        for s in SCENARIOS:
            blob = " ".join(
                [s.summary, s.failure_mode_today, *s.judging_criteria]
            ).lower()
            for phrase in banned:
                assert phrase not in blob, (s.scenario_id, phrase)
